############################# NOTE! ###############################
### If you are making any change to the client, please          ###
### update the version in shared.py.                            ###
### No two versions in prod should have the same version number.###
############################ THANKS! ##############################

import asyncio
import json
import shutil
import logging
import typing

from shared import VERSION

logger = logging.getLogger("mnbot")
del logging

import websockets
from websockets.asyncio.client import connect
import uuid_utils.compat as uuid

class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)
encoder = CustomEncoder()

class Websocket:
    def __init__(self, url: str, advisory_handler):
        self.advisory_handler = advisory_handler
        self.conn = None
        self.seq = 0
        self.url = url
        self.lock = asyncio.Lock()

    async def _send_once(self, type, payload=None) -> tuple[int, dict]:
        async with self.lock:
            if not self.conn:
                logger.debug("creating connection")
                conn = await connect(self.url)
                await conn.send(encoder.encode({"v": VERSION, "p": 2, "num_slots": 1}))
                self.conn = conn
            self.seq += 1
            seq = self.seq
            message = {"type": type, "request": payload, "seq": seq}
            logger.debug(f"sending {type} message (seq = {seq})")
            await self.conn.send(encoder.encode(message))
            while True:
                resp = json.loads(await self.conn.recv())
                if resp['seq'] is None: # Advisory
                    logger.debug(f"advisory: {resp}")
                    await self.advisory_handler(resp)
                else:
                    logger.debug(f"response: {resp}")
                    break
            if resp['seq'] != seq:
                logger.error(f"Mismatched messages. req = {repr(message)}; resp = {repr(resp)}")
                # Mismatched messages
                # Something has gone wrong. The connection is probably
                # desynced. Raise an error so we get rid of it.
                await self.conn.close(1008, "Connection desync") # 1008 Policy violation
                self.conn = None
                raise RuntimeError("Connection desync; bailing out")
            if resp['status'] >= 500: # Server error
                logger.error(f"Server error: {repr(resp)}")
                raise RuntimeError("Server error")
            if resp['status'] >= 400: # Client error
                logger.error(f"Client error: {repr(resp)}")
                raise RuntimeError("Client error")
            return resp['status'], resp['payload']

    async def _send(self, type, payload=None):
        tries = 0
        while True:
            try:
                return await self._send_once(type, payload)
            except Exception as e:
                if isinstance(e, websockets.WebSocketException):
                    self.conn = None # Assume the connection is no longer usable
                if tries >= 10: # try our best not to fail
                    logger.fatal("Max tries reached for websocket, propagating exception")
                    raise
                sleep = 2 ** tries
                logger.exception(f"Websocket exception, sleeping for {sleep} seconds")
                await asyncio.sleep(sleep)
                tries += 1

    async def claim_item(self, slot: int) -> typing.Optional[tuple[dict, str]]:
        status, resp = await self._send("Item:claim", {"slot": slot})
        if status != 200:
            raise RuntimeError(f"Bad response from server: {status} {resp}")
        if resp['item']:
            return resp['item'], resp['info_url']
        return None

    async def fail_item(self, attempt_id: str, reason: str, fatal: bool):
        status, resp = await self._send(
            "Item:fail",
            {
                "attempt_id": attempt_id,
                "message": reason,
                "fatal": fatal,
            }
        )
        if status != 204:
            raise RuntimeError(f"Bad response from server: {status} {resp}")

    async def finish_item(self, attempt_id: str):
        status, resp = await self._send("Item:finish", {"attempt_id": attempt_id})
        if status != 204:
            raise RuntimeError(f"Bad response from server: {status} {resp}")

    async def store_result(self, attempt_id: str, result_type: str, result):
        result_id = uuid.uuid7()
        pl = {
            "result_id": result_id,
            "attempt_id": attempt_id,
            "type": result_type,
            "payload": result,
        }
        status, resp = await self._send("Item:store", pl)
        if status != 201:
            raise RuntimeError(f"Bad response from server: {status} {resp}")

    async def ping(self):
        space = shutil.disk_usage("/")
        payload = dict(free = space.free, used = space.used, total = space.total)
        status, resp = await self._send("System:ping", {"disk": payload})
        if status != 200:
            raise RuntimeError(f"Bad response from server: {status} {resp}")
