#!/usr/bin/env python3

### HEY YOU! Yeah, you!
### If you are making any change to the client, please
### update the version in meta.py.
### No two versions in prod should have the same version number.

import enum
import asyncio, logging, random, logging, os, io, signal

logging.basicConfig(format = "%(asctime)s %(levelname)s <:%(thread)s> : %(message)s", level = logging.INFO)
logger = logging.getLogger("mnbot")

from meta import DEBUG
if DEBUG:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)
del logging

from tracker import Websocket

from rethinkdb import r

import browse
r.set_loop_type("asyncio")

CONNECT = os.environ['TRACKER_URL']

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
    brozzler = browse.Brozzler(MAX_WORKERS)

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
            logger.debug(f"spinning up worker")
            resp = await ws.claim_item()
            if resp:
                item, info_url = resp
                id = item['id']
                logger.info(f"Starting task {id}")
                url = item['item']
                assert "_" not in id
                prefix = "mnbot-brozzler-" + id.replace("-", "_")
                task = asyncio.create_task(brozzler.run_job(
                    ws,
                    item,
                    url,
                    prefix,
                    item['metadata']['stealth_ua'],
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
            except Exception:
                logger.exception(f"failed task {id}:")
                fmt = io.StringIO()
                finished_task.print_stack(file = fmt)
                await ws.fail_item(id, f"Caught exception!\n{fmt.getvalue()}", tries)
            else:
                logger.debug("task was successful!")
                await ws.finish_item(id)
                logger.debug("creating cleanup task")
                task = asyncio.create_task(browse.warcprox_cleanup())
                workers[task] = (TaskType.CLEANUP, None)
    pt.cancel()

asyncio.run(main())
