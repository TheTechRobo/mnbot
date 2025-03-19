import asyncio
import collections
import os
import json
import hmac
import typing
import logging

from rethinkdb import r
from websockets.asyncio.server import ServerConnection, basic_auth, serve
from rue import Entry, Queue, Status
from bot2h import SendOnlyBot

logging.basicConfig(level=logging.INFO)

INFO_URL = os.environ['INFO_URL']
TRACKER_BASE_URL = os.environ['TRACKER_BASE_URL'].rstrip("/")

def item_url(id: str | None):
    if not id:
        return "<N/A>"
    return f"{TRACKER_BASE_URL}/item/{id}"

r.set_loop_type("asyncio")
bot = SendOnlyBot(os.environ['H2IBOT_POST_URL'])

QUEUE = Queue("mnbot")
asyncio.run(QUEUE.check())

async def notify_user(item: Entry, message: str):
    url = item_url(item.id)
    await bot.send_message(f"{item.queued_by}: Your job {item.id} for {item.item} {message} See {url} for more information.")

HANDLERS = {}

def handler(msg_type: str):
    def decorator(f: typing.Callable):
        assert msg_type not in HANDLERS
        HANDLERS[msg_type] = f
        return f
    return decorator

Response: typing.TypeAlias = tuple[int, typing.Optional[dict[str, typing.Any]]]
HandlerContext = collections.namedtuple("HandlerContext", ["message", "username", "version"])

@handler("System:ping")
async def pong(ctx: HandlerContext) -> Response:
    await QUEUE.heartbeat(ctx.username)
    return 204, None

@handler("Item:claim")
async def get(ctx: HandlerContext, *, pipeline_type) -> Response:
    item = await QUEUE.claim(ctx.username, pipeline_type, ctx.version)
    if item:
        payload = {
            "item": item.as_json_friendly_dict() | {"_current_attempt": item.current_attempt()},
            "info_url": INFO_URL
        }
    else:
        payload = {
            "item": None,
            "message": "No items found."
        }
    return 200, payload

@handler("Item:fail")
async def fail(ctx: HandlerContext, *, id, message, attempt) -> Response:
    item = await QUEUE.get(id)
    if not item:
        raise Exception("item does not exist")
    new_item = await QUEUE.fail(item, f"Pipeline {ctx.username} reported failure: {message}", attempt)
    if new_item.status == Status.ERROR:
        await notify_user(new_item, "has failed.")
    return 204, None

@handler("Item:store")
async def store(ctx: HandlerContext, *, id, result, attempt, result_type):
    item = await QUEUE.get(id)
    if not item:
        raise Exception("item does not exist")
    new_item = await QUEUE.store_result(item, attempt, result, result_type)
    return 201, {"new_id": new_item}

@handler("Item:finish")
async def finish(ctx: HandlerContext, *, id) -> Response:
    item = await QUEUE.get(id)
    if not item:
        raise Exception("item does not exist")
    new_item = await QUEUE.finish(item)
    await notify_user(new_item, "has finished.")
    return 204, None

async def handle_connection(websocket: ServerConnection):
    initial = json.loads(await websocket.recv())
    version = initial['v']
    async for message in websocket:
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
                ctx = HandlerContext(message = message, username = websocket.username, version = version)
                # TODO: Detect when payload params don't match up
                #       and return 400
                status, payload = await callback(ctx, **message.get("request") or {})
                reply = {"status": status, "payload": payload, "seq": seq}
            except Exception:
                logging.exception(f"Exception occured while handling message {message}")
                reply = {"status": 500, "message": "An exception occured.", "seq": seq}
            logging.info(f"Handled {repr(type)} message from {websocket.username} with code {reply['status']}")
            await websocket.send(json.dumps(reply))
        else:
            await websocket.send(json.dumps({"status": 404, "message": f"Request type {type} does not exist", "seq": seq}))

async def authenticate(username, key):
    conn = await r.connect(host = os.getenv("RUE_DB_HOST", "localhost"))
    try:
        expected_key = await r.db("mnbot").table("server_secrets").get(username).run(conn)
        return expected_key and hmac.compare_digest(key, expected_key['value'])
    finally:
        try:
            await conn.close()
        except Exception:
            pass

authenticator = basic_auth(
    realm = "mnbot item server",
    check_credentials = authenticate
)

async def main():
    async with serve(
        handle_connection,
        "0.0.0.0", 8897,
        process_request = authenticator,
        max_size=2**25
    ) as server:
        await server.serve_forever()

asyncio.run(main())
