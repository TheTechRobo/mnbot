import asyncio
import base64
import collections
import time
import os
import json
import typing
import logging

import urlcanon

from ..common import model, db

from websockets.asyncio.server import ServerConnection, basic_auth, serve
from bot2h import Format, SendOnlyBot, Colour

import sqlalchemy

#db.logger.setLevel(logging.INFO)
logging.basicConfig(level = logging.INFO, format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s")

INFO_URL = os.environ['INFO_URL']
TRACKER_BASE_URL = os.environ['TRACKER_BASE_URL'].rstrip("/")

def item_url(id: model.UUID):
    return f"{TRACKER_BASE_URL}/job/{id}"

bot = SendOnlyBot(os.environ['H2IBOT_POST_URL'])

def notify_user(job_id: model.UUID, initial_item: str, author: str, message: str):
    url = item_url(job_id)
    return f"{author}: Your job {job_id} for {initial_item} {message} See {url} for more information."

HANDLERS = {}

def handler(msg_type: str):
    def decorator(f: typing.Callable):
        assert msg_type not in HANDLERS
        HANDLERS[msg_type] = f
        return f
    return decorator

Response: typing.TypeAlias = tuple[int, typing.Optional[dict[str, typing.Any]]]
HandlerContext = collections.namedtuple("HandlerContext", ["message", "pipeline", "version"])

PIPELINE_HEALTH = {}

@handler("System:ping")
async def pong(ctx: HandlerContext, disk: dict) -> Response:
    PIPELINE_HEALTH[ctx.pipeline.pipeline_id] = {"ping": int(time.time()), "disk": disk}
    return 204, None

@handler("Item:claim")
async def get(ctx: HandlerContext, *, slot) -> Response:
    pipeline: db.Pipeline = ctx.pipeline
    notify_message = None

    async with pipeline.parent.conn.begin():
        try:
            claim = await pipeline.find_claim_page(ctx.version, slot)
        except db.JobExhausted as e:
            claim = None
            ns = await pipeline.parent.update_job_status(e.job_id)
            q = sqlalchemy.select(model.jobs.c.initial_page, model.jobs.c.created_by).where(model.jobs.c.job_id == e.job_id)
            row = (await pipeline.conn.execute(q)).one()
            if ns == model.JobStatus.DONE:
                notify_message = notify_user(e.job_id, row[0], row[1], "has finished.")

    if notify_message:
        await bot.send_message(notify_message)

    if claim:
        payload = {
            "item": claim.as_json_friendly_dict(),
            "info_url": INFO_URL
        }
    else:
        payload = {
            "item": None,
            "message": "No items found."
        }
    return 200, payload

RED = Colour.make_colour(Colour.RED, escape = False)
RESET = Format.RESET
MONO = Format.MONOSPACE
@handler("Item:fail")
async def fail(ctx: HandlerContext, *, attempt_id, message, fatal) -> Response:
    attempt_id = db.parse_id(attempt_id)
    pipeline: db.Pipeline = ctx.pipeline
    notify_message = None

    # Must commit before sending any notification message, otherwise
    # serialization failures may cause excess or incorrect notifications.
    async with pipeline.parent.conn.begin():
        tries_remaining = await pipeline.fail_attempt(attempt_id, message, fatal)

        if tries_remaining <= 0:
            job_id, (initial_item, author) = await pipeline.parent.attempt_id_to_job_id(attempt_id, [model.jobs.c.initial_page, model.jobs.c.created_by])
            new_status = await pipeline.parent.update_job_status(job_id)
            if new_status == model.JobStatus.DONE:
                if await pipeline.parent.is_single_job(job_id):
                    summary = message.split("\n", 1)[0].strip()
                    notify_message = notify_user(job_id, initial_item, author, f"has {RED}failed{RESET} (last error: {MONO}{summary}{RESET}).")
                else:
                    notify_message = notify_user(job_id, initial_item, author, "has finished.")

    if notify_message:
        await bot.send_message(notify_message)
    return 204, None

@handler("Item:store")
async def store(ctx: HandlerContext, *, result_id, attempt_id, type, payload):
    result_id = db.parse_id(result_id)
    attempt_id = db.parse_id(attempt_id)
    pipeline: db.Pipeline = ctx.pipeline
    if type == "cjs_screenshot":
        type = "custom_js_screenshot"
    await pipeline.create_result(attempt_id, result_id, model.ResultType[type.upper()], payload)
    aux = {}
    if type == "outlinks":
        q = (
            sqlalchemy.select(model.job_rulesets, model.pages.c.job_id, model.pages.c.page_id)
            .select_from(model.attempts)
            .where(model.attempts.c.attempt_id == attempt_id)
            .join(model.job_rulesets, model.job_rulesets.c.job_ruleset_id == model.attempts.c.ruleset_id)
            .join(model.pages, model.pages.c.page_id == model.attempts.c.page_id)
        )
        row = (await pipeline.conn.execute(q)).one()
        ruleset = db.JobRuleset.from_row(row)
        accepted_urls = set()
        for url in payload:
            settings = db.PageSettings.from_ruleset(url, ruleset)
            if settings.accept:
                url = urlcanon.parse_url(url)
                urlcanon.canon.remove_fragment(url)
                accepted_urls.add(str(url))
        await pipeline.parent.create_pages(row.job_id, (db.PageCreation(db.generate_id(), i, row.page_id) for i in accepted_urls))
        aux = {"urls_added": len(accepted_urls)}

    return 201, {"new_id": str(result_id)} | aux

@handler("Item:finish")
async def finish(ctx: HandlerContext, *, attempt_id) -> Response:
    attempt_id = db.parse_id(attempt_id)
    pipeline: db.Pipeline = ctx.pipeline
    notify_message = None

    async with pipeline.parent.conn.begin():
        await pipeline.finish_attempt(attempt_id)
        job_id, (initial_item, author) = await pipeline.parent.attempt_id_to_job_id(attempt_id, [model.jobs.c.initial_page, model.jobs.c.created_by])
        new_status = await pipeline.parent.update_job_status(job_id)
        if new_status == model.JobStatus.DONE:
            notify_message = notify_user(job_id, initial_item, author, "has finished.")

    if notify_message:
        await bot.send_message(notify_message)
    return 204, None

async def handle_connection(websocket: ServerConnection):
    initial = json.loads(await websocket.recv())
    version = initial['v']
    protocol = initial.get("p", 1)
    if protocol != 2:
        await websocket.close(reason = "Protocol mismatch! Please update your client")
    slots = list(range(0, initial['num_slots']))
    async with ENGINE.connect() as conn:
        queue = db.Connection(conn)
        pipeline = await queue.pipeline(websocket.username, *slots)
        await conn.commit()
        async for message in websocket:
            start_time = time.time()
            try:
                message = json.loads(message)
                type = message['type']
                seq = message['seq']
            except KeyError:
                await websocket.send(json.dumps({"status": 400, "message": "Missing message data"}))
                continue
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"status": 400, "message": "Invalid JSON"}))
                continue
            if callback := HANDLERS.get(type):
                try:
                    ctx = HandlerContext(message = message, pipeline = pipeline, version = version)
                    # TODO: Detect when payload params don't match up
                    #       and return 400
                    status, payload = await callback(ctx, **message.get("request") or {})
                    reply = {"status": status, "payload": payload, "seq": seq}
                    if conn.in_transaction():
                        await conn.commit()
                except Exception:
                    logging.exception(f"Exception occured while handling message {message}")
                    reply = {"status": 500, "message": "An exception occured.", "seq": seq}
                    await conn.rollback()
                elapsed = round(time.time() - start_time, 1)
                logging.info(f"Handled {repr(type)} message from {websocket.username} with code {reply['status']} (in {elapsed}s)")
                await websocket.send(json.dumps(reply))
            else:
                await websocket.send(json.dumps({"status": 404, "message": f"Request type {type} does not exist", "seq": seq}))

async def authenticate(username, key):
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        queue = db.Connection(conn)
        pipeline = await queue.pipeline(username)
        try:
            await pipeline.authenticate(key)
        except db.AuthenticationFailure:
            return False
        else:
            return True

authenticator = basic_auth(
    realm = "mnbot item server",
    check_credentials = authenticate
)

async def request_hook(conn: ServerConnection, request):
    if request.path == "/health":
        res = {"status": 200, "pipelines": PIPELINE_HEALTH}
        r = conn.respond(200, json.dumps(res))
        del r.headers['Content-Type']
        r.headers['Content-Type'] = "application/json"
        return r
    return await authenticator(conn, request)

async def main():
    global ENGINE
    ENGINE = await db.create_engine()
    async with serve(
        handle_connection,
        "0.0.0.0", 8897,
        process_request = request_hook,
        max_size=2**25
    ) as server:
        await server.serve_forever()

asyncio.run(main())
