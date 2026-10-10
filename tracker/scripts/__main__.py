import argparse
import asyncio
import sys

from ..common import model
from ..common import db

import secrets
import string

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

if __name__ == "__main__":
    args = parser.parse_args()
    asyncio.run(args.func(args))
