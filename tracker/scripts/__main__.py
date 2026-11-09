import argparse
import asyncio
import secrets
import string
import time

import uuid_utils
import sqlalchemy

from ..common import model
from ..common import db

parser = argparse.ArgumentParser()
parser.add_argument("--uri", default = None, help = "URI to a postgres database; default is the value of MNBOT_DATABASE_URI")

subparsers = parser.add_subparsers(required = True)

async def create(args):
    if args.tries > 32767 or args.tries < 0:
        raise ValueError("Invalid value of tries, must fit within the positive range of a SMALLINT")
    engine = await db.create_engine(args.uri, check_version = False)
    async with engine.connect() as conn:
        await conn.run_sync(model.metadata_obj.create_all)
        await conn.execute(model.options.insert().values(key = "version", value = str(model.SCHEMA_VERSION)))
        await conn.execute(model.options.insert().values(key = "tries", value = str(args.tries)))
        await conn.commit()
    await engine.dispose()

subparser_create = subparsers.add_parser("create")
subparser_create.add_argument("--tries", type = int)
subparser_create.set_defaults(func = create)

async def add_pipeline(args):
    engine = await db.create_engine(args.uri, check_version = True)
    async with engine.begin() as conn:
        queue = db.Connection(conn)
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for i in range(32))
        await queue.create_pipeline(args.id, args.matchonly, password)
        print("Created", "matchonly" if args.matchonly else "regular", "pipeline", args.id, "with password", password)
    await engine.dispose()

subparser_add_pipeline = subparsers.add_parser("add_pipeline")
subparser_add_pipeline.set_defaults(func = add_pipeline)
subparser_add_pipeline.add_argument("--matchonly", action = "store_true", default = False)
subparser_add_pipeline.add_argument("id")

async def delete_old_results(args):
    """
    Expires old results.

    To delete screenshots older than N days, use --screenshot-days N.
    If you are using this script, you may wish to create an index:
        CREATE INDEX screenshot_removal_cronjob ON results (result_id ASC) WHERE type = 'SCREENSHOT' and payload != 'null'::jsonb;

    To delete DOM snapshots older than N days, use --dom-days N.
    You may wish to create an index:
        CREATE INDEX dom_removal_cronjob ON results (result_id ASC) WHERE type = 'RENDERED_DOM';
    (The entire row is deleted, unlike a screenshot.)

    Intended for use in a cronjob.
    """
    engine = await db.create_engine(args.uri, check_version = True)
    async with engine.begin() as conn:
        if args.screenshot_days is not None:
            target_ts = int((time.time() - args.screenshot_days * 86400) * 1e+9)
            maxid = uuid_utils.uuid7(nanoseconds = target_ts)
            q = (
                sqlalchemy.update(model.results)
                .where(model.results.c.result_id < maxid)
                .where(model.results.c.type == model.ResultType.SCREENSHOT)
                .where(model.results.c.payload != sqlalchemy.text("'null'::jsonb"))
                .values(payload = sqlalchemy.text("'null'::jsonb"))
            )
            res = await conn.execute(q)
            print("Deleted", res.rowcount, "screenshots")
        if args.dom_days is not None:
            target_ts = int((time.time() - args.dom_days * 86400) * 1e+9)
            maxid = uuid_utils.uuid7(nanoseconds = target_ts)
            q = (
                sqlalchemy.delete(model.results)
                .where(model.results.c.result_id < maxid)
                .where(model.results.c.type == model.ResultType.RENDERED_DOM)
            )
            res = await conn.execute(q)
            print("Deleted", res.rowcount, "DOM snapshots")
    await engine.dispose()

subparser_dlos = subparsers.add_parser("delete_old_results")
subparser_dlos.set_defaults(func = delete_old_results)
subparser_dlos.add_argument("--screenshot-days", default = None, type = int)
subparser_dlos.add_argument("--dom-days", default = None, type = int)

if __name__ == "__main__":
    args = parser.parse_args()
    asyncio.run(args.func(args))
