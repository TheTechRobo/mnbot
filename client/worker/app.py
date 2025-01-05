#!/usr/bin/env python3

import asyncio, logging, random, logging, os, io, signal

logging.basicConfig(format = "%(asctime)s %(levelname)s <:%(thread)s> : %(message)s", level = logging.INFO)
logger = logging.getLogger("chromebot")

if os.environ.get("DEBUG") == "1":
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
    workers = dict()
    brozzler = browse.Brozzler(MAX_WORKERS)

    def handle_sigint():
        logger.debug("sigint")
        if not STOP.is_set():
            for task, val in workers.items():
                if not val:
                    # Sleep task; safe to cancel
                    logger.debug(f"cancelling {task}")
                    task.cancel()
                elif val.endswith("_cleanup"):
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
            item = await ws.claim_item()
            if item:
                id = item['id']
                logger.info(f"Starting task {id}")
                url = item['item']
                assert "_" not in id
                prefix = "chromebot-brozzler-" + id.replace("-", "_")
                task = asyncio.create_task(brozzler.run_job(
                    ws,
                    item,
                    url,
                    prefix,
                    item['metadata']['user_agent'],
                    item['metadata']['custom_js']
                ))
                task.set_name(id)
            else:
                to_sleep = random.randint(10, 30)
                logger.info(f"No items found, blocking this worker for {to_sleep} seconds.")
                task = asyncio.create_task(asyncio.sleep(to_sleep))
                id = None
            workers[task] = id
        done: set[asyncio.Task] = (await asyncio.wait(workers, return_when = asyncio.FIRST_COMPLETED))[0]
        for finished_task in done:
            logger.debug(f"checking finished task {finished_task}")
            id = workers[finished_task]
            del workers[finished_task]
            if not id or id.endswith("_cleanup"):
                logger.debug("nevermind, not an item")
                continue
            try:
                _dedup_bucket, _stats_bucket = finished_task.result()
            except Exception:
                logger.exception(f"failed task {id}:")
                fmt = io.StringIO()
                finished_task.print_stack(file = fmt)
                await ws.fail_item(id, f"Caught exception!\n{fmt.getvalue()}")
            else:
                logger.debug("task was successful!")
                await ws.finish_item(id)
                logger.debug("creating cleanup task")
                task = asyncio.create_task(browse.warcprox_cleanup())
                workers[task] = f"{id}_cleanup"
    pt.cancel()

asyncio.run(main())
