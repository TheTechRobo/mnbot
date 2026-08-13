import os
import asyncio

import aiohttp
import datetime
import validators
import traceback

import sqlalchemy

from bot2h import Bot, Colour, Format, User

from ..common import db, model

H2IBOT_GET_URL = os.environ['H2IBOT_GET_URL']
H2IBOT_POST_URL = os.environ['H2IBOT_POST_URL']
TRACKER_BASE_URL = os.environ['TRACKER_BASE_URL'].rstrip("/")
DOCUMENTATION_URL = os.environ['DOCUMENTATION_URL']
TRACKER_HOST = os.environ['TRACKER_HOST']
MNBOT_HEADER = "//! mnbot v1" # DO NOT ADD \n or \r\n here.

def is_mnbot_js(payload):
    """
    Returns true if the payload is a mnbot CJS header.
    """
    if not payload.startswith(MNBOT_HEADER): return False
    def validate_suffix(payload, suffix):
        return payload[len(MNBOT_HEADER):len(MNBOT_HEADER)+len(suffix)] == suffix
    return validate_suffix(payload, "\n") or validate_suffix(payload, "\r\n")

def item_url(id):
    if not id:
        return "<N/A>"
    return f"{TRACKER_BASE_URL}/job/{id}"

bot = Bot(H2IBOT_GET_URL, H2IBOT_POST_URL, max_coros = 1)

PRESET_USER_AGENTS = {
    "curl": "curl/7.88.1",
    "archivebot": (
        "ArchiveTeam ArchiveBot/20240923.203d40a (wpull 2.0.3) and not Mozilla/5.0 "
        "(Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/42.0.2311.90 Safari/537.36"
    ),
    "googlebot1": (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
}
DYNAMIC_USER_AGENTS = ("default", "stealth", "minimal", "googlebot")

class ValidationError(Exception):
    def __init__(self, url):
        self.url = url

class CustomMessageException(Exception):
    def __init__(self, msg):
        self.msg = msg

def select_ua(ua):
    if user_agent := PRESET_USER_AGENTS.get(ua):
        return  "$" + user_agent
    elif ua in DYNAMIC_USER_AGENTS:
        return ua
    else:
        raise ValueError("Invalid user agent selection")

async def fetch_custom_js(url):
    try:
        async with AIOHTTP_SESSION.get(url) as resp:
            if resp.status != 200:
                raise CustomMessageException(f"Failed to retrieve custom JS! Got status {resp.status} (expected 200).")
            custom_js = await resp.text()
            if not is_mnbot_js(custom_js):
                raise CustomMessageException("Error: Custom JS must start with a valid mnbot header.")
            return custom_js
    except Exception as e:
        print("Custom JS exception:")
        traceback.print_exc()
        raise CustomMessageException(f"Failed to retrieve custom JS ({type(e)} was raised).")

@bot.add_argument("--accept", default = None)
@bot.add_argument("--depth", default = "0")
@bot.add_argument("--concurrency", "-c", type = int, default = 1)
@bot.add_argument(
    "--user-agent", "-u",
    choices = list(DYNAMIC_USER_AGENTS) + list(PRESET_USER_AGENTS.keys()),
    default = "default"
)
@bot.add_argument("--explanation", "--explain", "-e")
@bot.add_argument("--custom-js")
@bot.add_argument("--skip-url-validation", action = "store_true")
@bot.add_argument("--nice", "-n", type = int, default = 0)
@bot.add_argument("url")
@bot.add_argument("redirector", nargs = "?", metavar = "<")
@bot.argparse("!brozzle")
@bot.command({"!b", "!brozzle"}, required_modes="+@")
async def brozzle(self: Bot, user: User, ran, args):
    try:
        if args.depth in ("inf", "infinity"):
            depth = None
        else:
            depth = int(args.depth)
            assert depth >= 0
    except (ValueError, AssertionError):
        raise CustomMessageException("Invalid depth value! Please supply either an integer >= 0 or 'inf' for no limit.")

    metadata = {}
    if args.nice < -10:
        if "@" not in user.modes:
            yield "Sorry, but only operators can queue with a niceness lower than -10."
            return
    if args.skip_url_validation:
        if "@" not in user.modes:
            yield "Sorry, but only operators can bypass URL validation."
            return

    cjs_config = None
    if args.custom_js:
        if "@" not in user.modes:
            yield "Sorry, but only operators can include custom JavaScript."
            return
        cjs_config = await fetch_custom_js(args.custom_js)
    ua_config = select_ua(args.user_agent)

    initial_ruleset = db.JobRuleset(
            db.generate_id(),
            ua = db.RulesetColumn(ua_config, []),
            custom_js = db.RulesetColumn(cjs_config, []),
            skip = db.RulesetColumn(False, []),
            accept = db.RulesetColumn(False, []),
    )
    if args.accept is not None:
        db.regex.compile(args.accept)
        initial_ruleset.accept.rules.append(db.JobRule(args.accept, True))

    job_id = db.generate_id()
    job = db.JobCreation(
        job_id = job_id,
        created_by = user.nick,
        metadata = metadata,
        initial_page = args.url,
        concurrency = args.concurrency,
        nice = args.nice,
        note = args.explanation,
        initial_ruleset = initial_ruleset,
        depth = depth,
    )

    async with ENGINE.connect() as conn:
        queue = db.Connection(conn)
        await queue.create_jobs([job])

        num_urls = 0
        async def submit_page_batch(pages):
            nonlocal num_urls
            data = []
            for page in pages:
                if not args.skip_url_validation:
                    result = validators.url(page, strict_query = False, private = False)
                    if result is not True:
                        raise ValidationError(page)
                data.append(db.PageCreation(page_id = db.generate_id(), payload = page, parent_page = None))
            res = await queue.create_pages(job_id, data)
            # create_pages returns a mapping of {given_id: actual_id}.
            # If given_id != actual_id, the page was already in the database.
            # (In this case, that means there was duplication in the list.)
            num_urls += sum(1 for key, value in res.items() if key == value)

        if args.redirector == "<":
            try:
                async with AIOHTTP_SESSION.get(args.url) as resp:
                    if resp.status != 200:
                        yield f"Failed to retrieve URL list! Got status {resp.status} (expected 200)."
                        return
                    buf = []
                    async for url in resp.content:
                        url = url.decode().rstrip("\r\n")
                        if not url:
                            continue
                        buf.append(url)
                        if len(buf) >= 50:
                            await submit_page_batch(buf)
                            buf = []
                    if buf:
                        await submit_page_batch(buf)
                        buf = []
            except ValidationError as e:
                raise
            except Exception as e:
                yield f"Failed to retrieve the URL list ({type(e)} was raised)."
                print("Retrieval exception:")
                traceback.print_exc()
                return
            if num_urls == 0:
                yield "Your list appears to be empty."
                return
            await conn.commit()
            yield f"Queued {num_urls} pages from {args.url} for Brozzler-based archival. You will be notified when it finishes. Use !status {job_id} or check {item_url(job_id)} for details."
        else:
            if args.redirector:
                yield "Sorry, but only one URL or URL list can be specified at a time."
                return
            await submit_page_batch([args.url])
            await conn.commit()
            yield f"Queued {args.url} for Brozzler-based archival. You will be notified when it finishes. Use !status {job_id} or check {item_url(job_id)} for details."

@bot.add_argument("--add-before", default = None, type = int)
@bot.add_argument("--ensure-ruleset", default = None)
@bot.add_argument("arg", nargs = "?")
@bot.add_argument("pattern")
@bot.add_argument("setting", choices = ("ua", "user_agent", "custom_js", "skip", "no_skip", "accept", "reject"))
@bot.add_argument("job_id")
@bot.argparse("!addrule")
@bot.command({"!addrule"}, required_modes = "+@")
async def addrule(self: Bot, user: User, ran, args):
    if args.setting in ("skip", "no_skip", "accept", "reject") and args.arg:
        yield f"The '{args.setting}' setting does not accept arguments."
        return
    if args.setting in ("ua", "custom_js") and not args.arg:
        yield f"The '{args.setting}' setting requires an additional argument."
        return
    # Ensure the regex can be compiled
    db.regex.compile(args.pattern)

    async with ENGINE.begin() as conn:
        queue = db.Connection(conn)
        if args.setting in ("skip", "no_skip"):
            key = "skip"
            payload = (args.setting == "skip")
        elif args.setting in ("accept", "reject"):
            key = "accept"
            payload = (args.setting == "accept")
        elif args.setting in ("ua", "user_agent"):
            key = "ua"
            try:
                payload = select_ua(args.arg)
            except ValueError:
                raise CustomMessageException("Sorry, but that is not a valid user agent. Choices include: " + ", ".join(PRESET_USER_AGENTS.keys()) + ", ".join(DYNAMIC_USER_AGENTS))
        elif args.setting == "custom_js":
            if "@" not in user.modes:
                raise CustomMessageException("Sorry, but only operators can include custom JavaScript.")
            key = "custom_js"
            payload = await fetch_custom_js(args.arg)
        else:
            raise RuntimeError("Unreachable code")
        nid, nidx = await queue.create_job_rule(args.job_id, key, args.add_before, db.JobRule(args.pattern, payload), args.ensure_ruleset)
    yield f"Created new {key} rule at index {nidx} (new ruleset ID: {nid})."

@bot.add_argument("--index", action = "store_true")
@bot.add_argument("--ensure-ruleset", default = None)
@bot.add_argument("pattern_or_index")
@bot.add_argument("setting", choices = ("ua", "user_agent", "custom_js", "skip", "accept"))
@bot.add_argument("job_id")
@bot.argparse("!delrule")
@bot.command("!delrule", required_modes = "+@")
async def delrule(self: Bot, user: User, ran, args):
    key = args.setting
    if key == "user_agent":
        key = "ua"
    async with ENGINE.begin() as conn:
        queue = db.Connection(conn)
        if args.index:
            ruleset_id, old_rule = await queue.remove_job_rule(args.job_id, key, int(args.pattern_or_index), args.ensure_ruleset)
            message = f"Removed {key} rule {old_rule} (new ruleset ID: {ruleset_id})."
        else:
            ruleset_id, num_removed = await queue.remove_job_rules_by_scope(args.job_id, key, args.pattern_or_index, args.ensure_ruleset)
            if num_removed == 0:
                raise CustomMessageException("No rule with that pattern was found.")
            s = "" if num_removed == 1 else "s"
            message = f"Removed {num_removed} {key} rule{s} (new ruleset ID: {ruleset_id})."
    yield message

@bot.command({"!concurrency", "!con"}, required_modes = "+@")
async def concurrency(self: Bot, user: User, ran, job_id, num):
    job_id = db.parse_id(job_id)
    try:
        num = int(num)
        assert num >= 0
    except (ValueError, AssertionError):
        yield "Sorry, but concurrency must be a positive integer."
        return
    async with ENGINE.begin() as conn:
        q = sqlalchemy.update(model.jobs).where(model.jobs.c.job_id == job_id).values(concurrency = num)
        await conn.execute(q)
    yield f"Updated concurrency of {job_id} to {num}."

async def generate_status_message(job: str, queue: db.Connection):
    q = sqlalchemy.select(model.jobs.c.status, model.jobs.c.initial_page, model.jobs.c.note)
    ts = db.parse_id_ex(job).timestamp / 1000
    date = datetime.datetime.fromtimestamp(ts, datetime.UTC)
    res = await queue.conn.execute(q)
    ent = res.first()
    if not ent:
        return f"No job with ID {repr(job)} could be found."
    return f"Job {job} ({repr(ent[1])}) has status {ent[0].name} and was queued at {date.isoformat(timespec='seconds')}. See {item_url(job)} for more information. Explanation: {ent[2]}"

async def health_check(queue: db.Connection):
    status = await queue.get_pipeline_health_status()
    match status:
        case db.PipelineHealthStatus.HEALTHY:
            health = "All pipelines report being healthy."
        case db.PipelineHealthStatus.DEGRADED:
            health = "Some pipelines report being unhealthy."
        case db.PipelineHealthStatus.UNHEALTHY:
            health = "All pipelines are unhealthy!"
    return f"{health} See {TRACKER_BASE_URL}/pipelines for more information."

@bot.command("!status")
async def status(self: Bot, user: User, ran, *jobs):
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        queue = db.Connection(conn)
        if jobs:
            for job in jobs:
                yield await generate_status_message(job, queue)
        else:
            yield await health_check(queue)
            count = await queue.get_job_counts()
            if not count:
                yield "There aren't any queued or running jobs."
            else:
                yield f"There are currently {count} active jobs."

@bot.command("!df")
async def df(self: Bot, user: User, ran):
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        yield await health_check(db.Connection(conn))

@bot.command({"!explain", "!e"}, required_modes = "+@")
async def explain(self: Bot, user: User, ran, id, *reason):
    id = db.parse_id(id)
    async with ENGINE.begin() as conn:
        r = " ".join(reason) or None
        q = sqlalchemy.update(model.jobs).where(model.jobs.c.job_id == id).values(note = r)
        res = await conn.execute(q)
        if res.rowcount:
            yield f"Reason for {id} set to {r!r}."
        else:
            yield "No item was found."

@bot.command("!tag")
async def tag(self: Bot, user: User, ran, command: str, pipeline_id: str, tag = None):
    async with ENGINE.connect() as conn:
        if command == "list":
            if tag:
                yield "Too many arguments for !tag list."
                return
            conn = await conn.execution_options(postgresql_readonly = True)
            queue = db.Connection(conn)
            try:
                pipeline = await queue.pipeline(pipeline_id)
            except db.NoSuchPipelineError:
                yield f"Pipeline {pipeline_id} does not exist."
                return
            tags = await pipeline.get_tags()
            if tags:
                message = f"Pipeline {pipeline_id} has the following tags: "
                message += ", ".join(tags)
            else:
                message = f"Pipeline {pipeline_id} has no tags."
            yield message
        elif not tag:
            yield "A tag must be provided."
            return
        else:
            if "@" not in user.modes:
                yield "Sorry, but only operators can modify tags."
                return
            queue = db.Connection(conn)
            try:
                pipeline = await queue.pipeline(pipeline_id)
            except db.NoSuchPipelineError:
                yield f"Pipeline {pipeline_id} does not exist."
                return
            if command == "add":
                await pipeline.create_tags(tag)
                await conn.commit()
                yield f"Added {tag} to pipeline {pipeline_id}."
            elif command == "remove":
                res = await pipeline.remove_tags(tag)
                await conn.commit()
                if res:
                    yield f"Removed {tag} from pipeline {pipeline_id}."
                else:
                    yield f"Tag {tag} was not found on pipeline {pipeline_id}, no action was taken."
            else:
                yield "Invalid subcommand."

@bot.command("!help")
async def help(self: Bot, user: User, ran, command = None):
    yield f"Documentation can be found at {DOCUMENTATION_URL}."

@bot.command("!page")
async def page(self: Bot, user: User, ran, page_id, action, arg = None):
    page_id = db.parse_id(page_id)
    async with ENGINE.connect() as conn:
        queue = db.Connection(conn)
        if arg:
            if "+" not in user.modes and "@" not in user.modes:
                yield "Sorry, but only voiced users can update page metadata."
                return
            if action == "status":
                ns = model.PageStatus[arg.upper()]
                q = sqlalchemy.update(model.pages).where(model.pages.c.page_id == page_id).values(status = ns)
                await conn.execute(q)
                await conn.commit()
                yield f"Updated {page_id} to status {ns.name}."
            elif action == "tries":
                try:
                    nt = int(arg)
                except ValueError:
                    yield f"Invalid integer {arg}."
                    return
                await queue.retry_page(page_id, nt)
                await conn.commit()
                yield f"Cleared attempt counter and set maximum of {nt} tries for {page_id}."
                return
            else:
                yield f"{action} is not a valid query."
                return
        else:
            conn = await conn.execution_options(postgres_readonly = True)
            if action == "status":
                q = sqlalchemy.select(model.pages.c.status).where(model.pages.c.page_id == page_id)
                res = await conn.scalar(q)
                if not res:
                    yield f"Page {page_id} does not exist."
                    return
                yield f"Page {page_id} has status {res.name}."
                return
            elif action == "tries":
                q = sqlalchemy.select(model.pages.c.attempts, model.pages.c.attempts_remaining).where(model.pages.c.page_id == page_id)
                result = await conn.execute(q)
                res = result.first()
                if not res:
                    yield f"Page {page_id} does not exist."
                    return
                yield f"Since last reset, page {page_id} has been tried {res[0]} out of {res[0] + res[1]} allowed attempts."
                return
            else:
                yield f"{action} is not a valid query."
                return

RED = Colour.make_colour(Colour.RED)
@bot.exception_handler
async def handler(self: Bot, command, user: User, e):
    if isinstance(e, db.InvalidIdError):
        return f"{user.nick}: {RED}Invalid UUID."
    elif isinstance(e, ValidationError):
        return f"{user.nick}: {RED}Failed to validate URL {repr(e.url)}, cowardly bailing out.{Format.RESET} (Ops may use --skip-url-validation to bypass this.)"
    elif isinstance(e, db.SerializationFailure):
        return f"{user.nick}: {RED}Serialization failure! Please try again."
    elif isinstance(e, CustomMessageException):
        return f"{user.nick}: {e.msg}"
    elif isinstance(e, db.regex.error):
        return f"{user.nick}: Regex compilation error! Note: mnbot uses the Python 'regex' module."
    else:
        print("Exception occurred!")
        traceback.print_exc()
        return f"{user.nick}: {RED}An error occurred while processing the command."

async def main():
    global ENGINE, AIOHTTP_SESSION
    ENGINE = await db.create_engine()
    AIOHTTP_SESSION = aiohttp.ClientSession()
    await bot.run_forever()

if __name__ == "__main__":
    asyncio.run(main())

