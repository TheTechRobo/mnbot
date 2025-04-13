#!/usr/bin/env python3

############################# NOTE! ###############################
### If you are making any change to the client, please          ###
### update the version in shared.py.                            ###
### No two versions in prod should have the same version number.###
############################ THANKS! ##############################

import asyncio
import concurrent.futures
import dataclasses
import enum
import io
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import typing
import urllib.request

import aiofiles

PYTHON = shutil.which("python3")
assert PYTHON, "Could not find the Python interpreter."

logger = logging.getLogger("mnbot")

from shared import DEBUG, PROXY_URL, Job
if DEBUG:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)
del logging

from tracker import Websocket

from rethinkdb import r

CONNECT = os.environ['TRACKER_URL']

class ItemFailedException(Exception):
    """
    Raised to provide a custom failure message for the item.
    """
    def __init__(self, msg: str, fatal: bool):
        self.msg = msg
        self.fatal = fatal

# Wait for warcprox to start
while True:
    try:
        with urllib.request.urlopen("http://" + PROXY_URL.rstrip("/") + "/status") as conn:
            if conn.getcode() == 200:
                break
            else:
                logger.info("warcprox isn't healthy yet, sleeping")
                time.sleep(5)
    except Exception:
        logger.info("no warcprox yet, sleeping")
        time.sleep(5)

# Convenience function to get the dedup DB
def _dedup_db():
    return r.db("cb-warcprox").table("dedup")

# Add our index
def _setup_db():
    conn = r.connect(host = "rethinkdb")
    indexes = _dedup_db().index_list().run(conn)
    #if "mnbot-bucket" not in indexes:
    #    _dedup_db().index_create("mnbot-bucket", lambda row : row['key'].split("|", 1)).run(conn)
    if "mnbot-date" not in indexes:
        _dedup_db().index_create("mnbot-date", r.iso8601(r.row['date'])).run(conn)
    conn.close()
_setup_db()
r.set_loop_type("asyncio")

async def warcprox_cleanup():
    conn = None
    try:
        conn = await r.connect(host = "rethinkdb")
        # We don't currently clean up the stats bucket as warcprox does it in batches, so when
        # this function runs, warcprox is going to re-add it in a few seconds anyway.
        logger.debug("deleting old dedup records")
        res = await (
            _dedup_db()
            # Deletes records more than 7 days old, to prevent the database from blowing up
            # and to make sure that corrupt records don't forever cause a URL to be lost
            .between(r.minval, r.now() - 7*24*3600, index = "mnbot-date")
            .delete(durability = "soft")
            .run(conn)
        )
        logger.debug(f"queued {res['deleted']} old dedup records for deletion")
    except Exception:
        logger.exception(f"failed to clean up old records")
    finally:
        try:
            if conn:
                await conn.close()
        except Exception:
            pass

async def run_job(ws: Websocket, full_job: dict, url: str, warc_prefix: str, ua: str, custom_js: typing.Optional[str], info_url: str):
    tries = full_job['_current_attempt']
    id = full_job['id']
    #dedup_bucket = f"dedup-{id}-{tries}"
    dedup_bucket = ""
    stats_bucket = f"stats-{id}-{tries}"
    job = Job(
        full_job = full_job,
        url = url,
        warc_prefix = warc_prefix,
        dedup_bucket = dedup_bucket,
        stats_bucket = stats_bucket,
        ua = ua,
        custom_js = custom_js,
        cookie_jar = None,
        mnbot_info_url = info_url
    )
    job_data = json.dumps(dataclasses.asdict(job))
    pread, pwrite = os.pipe()
    assert PYTHON
    process = await asyncio.create_subprocess_exec(
        PYTHON,
        os.path.join(os.path.dirname(sys.argv[0]), "browse.py"),
        str(pwrite),
        id, # useful for ps
        stdin = subprocess.PIPE,
        stdout = subprocess.PIPE,
        stderr = subprocess.STDOUT,
        pass_fds = (pwrite,),
    )
    os.close(pwrite)

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        async with aiofiles.open(pread, "r", executor = pool) as pread:
            # This won't deadlock because we know that browse.py will
            # read stdin before writing anything.
            stdout = process.stdout
            stdin = process.stdin
            assert stdin and stdout
            stdin.write(job_data.encode() + b"\n")
            await stdin.drain()
            stdin.close()

            # This bit of the code is a little ugly since we can't cancel aiofiles' coroutines.
            # The general idea is that we create a set with coroutines awaiting the
            # next lines of both stdout and pread, and then replenish whichever one completed.
            # Continue doing that until EOF.
            coros = set((
                asyncio.create_task(stdout.readline(), name = "stdout"),
                asyncio.create_task(pread.readline(), name = "pread"),
            ))
            while coros:
                done, pending = await asyncio.wait(coros, return_when = asyncio.FIRST_COMPLETED)
                for coro in done:
                    res = await coro
                    if not res:
                        # EOF reached, don't create another task of this type
                        continue
                    if coro.get_name() == "stdout":
                        if res := res.decode().strip():
                            print(f"stdout[{id}]: {res}", flush = True)
                        pending.add(asyncio.create_task(stdout.readline(), name = "stdout"))
                    elif coro.get_name() == "pread":
                        pending.add(asyncio.create_task(pread.readline(), name = "pread"))
                        res = json.loads(res)
                        type = res['type']
                        payload = res['payload']
                        if type in ("status_code", "outlinks", "final_url", "requisites", "custom_js"):
                            await ws.store_result(id, type, tries, payload)
                        elif type == "screenshot":
                            await ws.store_result(id, type, tries, payload, ("full", "thumb"))
                        elif type == "error":
                            raise ItemFailedException(f"Subprocess reported error: {payload}", False)
                        elif type == "fatal":
                            raise ItemFailedException(f"Subprocess reported error: {payload}", True)
                        else:
                            logger.error(f"unrecognized message: {res}")
                    else:
                        raise RuntimeError(f"invalid coro type! {coro}")
                coros = pending
        code = await process.wait()
        if code != 0:
            raise RuntimeError(f"Exited with code {code}")
    return dedup_bucket, stats_bucket

STOP = asyncio.Event()

class TaskType(enum.Enum):
    SLEEP = enum.auto()
    CLEANUP = enum.auto()
    ITEM = enum.auto()

async def main():
    async def handler(advisory: dict):
        print("Received advisory", advisory)

    ws = Websocket(CONNECT, handler)

    async def ping_occasionally():
        # Allows advisories to keep being received
        # Could also do a non-blocking recv loop, which gets rid of the need for sending the ping,
        # but keeping the connection alive is useful.
        while True:
            try:
                await ws.ping()
            except Exception:
                pass
            await asyncio.sleep(15)

    MAX_WORKERS = 1
    workers: dict[asyncio.Task, tuple[TaskType, dict | None]] = dict()

    def handle_sigint():
        logger.debug("sigint")
        if not STOP.is_set():
            for task, (task_type, _val) in workers.items():
                if task_type == TaskType.SLEEP:
                    # Sleep task; safe to cancel
                    logger.debug(f"cancelling {task}")
                    task.cancel()
                elif task_type == TaskType.CLEANUP:
                    # Cleanup task; not ideal to cancel
                    pass
                else:
                    # Item task; not safe to cancel
                    pass
            logger.info("Stopping when current tasks are complete...")
        STOP.set()

    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, handle_sigint)

    pt = asyncio.create_task(ping_occasionally())
    while True:
        if STOP.is_set() and not workers:
            logger.info("no more workers to wait on, breaking")
            break
        assert len(workers) <= MAX_WORKERS
        for _ in range(MAX_WORKERS - len(workers)):
            if STOP.is_set():
                logger.debug("not spinning up new item as we are pending a stop")
                continue
            logger.debug("spinning up worker")
            resp = await ws.claim_item()
            if resp:
                item, info_url = resp
                id = item['id']
                logger.info(f"Starting task {id}")
                url = item['item']
                assert "_" not in id
                prefix = "mnbot-brozzler-" + id.replace("-", "_")
                task = asyncio.create_task(run_job(
                    ws,
                    item,
                    url,
                    prefix,
                    item['metadata']['ua'],
                    item['metadata']['custom_js'],
                    info_url
                ))
                task.set_name(id)
                workers[task] = (TaskType.ITEM, item)
            else:
                to_sleep = random.randint(10, 30)
                logger.info(f"No items found, blocking this worker for {to_sleep} seconds.")
                task = asyncio.create_task(asyncio.sleep(to_sleep))
                workers[task] = (TaskType.SLEEP, None)
        done: set[asyncio.Task] = (await asyncio.wait(workers, return_when = asyncio.FIRST_COMPLETED))[0]
        for finished_task in done:
            logger.debug(f"checking finished task {finished_task}")
            task_type, item = workers[finished_task]
            del workers[finished_task]
            if task_type != TaskType.ITEM:
                logger.debug("nevermind, not an item")
                continue
            # item can't be None at this point
            id = item['id']
            tries = item['_current_attempt']
            try:
                _dedup_bucket, _stats_bucket = finished_task.result()
            except Exception as e:
                if isinstance(e, ItemFailedException):
                    message = e.msg
                    fatal = e.fatal
                else:
                    logger.exception(f"failed task {id}:")
                    fmt = io.StringIO()
                    finished_task.print_stack(file = fmt)
                    message = f"Caught exception!\n{fmt.getvalue()}"
                await ws.fail_item(id, message, tries, fatal)
            else:
                logger.debug("task was successful!")
                await ws.finish_item(id)
                logger.debug("creating cleanup task")
                task = asyncio.create_task(warcprox_cleanup())
                workers[task] = (TaskType.CLEANUP, None)
    pt.cancel()

asyncio.run(main())
