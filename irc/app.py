import os
import asyncio

import aiohttp
import validators

from bot2h import Bot, Colour, Format, User
from rue import Queue, Status

H2IBOT_GET_URL = os.environ['H2IBOT_GET_URL']
H2IBOT_POST_URL = os.environ['H2IBOT_POST_URL']

bot = Bot(H2IBOT_GET_URL, H2IBOT_POST_URL, max_coros = 1)

QUEUE = Queue("chromebot")
asyncio.run(QUEUE.check())
print("setup complete")

AIOHTTP_SESSION = None

@bot.add_argument("--user-agent")
@bot.add_argument("--explanation")
@bot.add_argument("--custom-js")
@bot.add_argument("url")
@bot.argparse("!brozzle")
@bot.command({"!b", "!brozzle"}, required_modes="+@")
async def brozzle(self: Bot, user: User, ran, args):
    """
    Brozzle a URL.
    --custom-js: run custom javascript when crawling; ops only
    --explanation: explanation/notes for the job; can be changed later
    --user-agent: user agent to use (verbatim)
    """
    global AIOHTTP_SESSION
    custom_js = None
    if args.custom_js:
        if "@" not in user.modes:
            yield "The --custom-js param can only be used by operators."
            return
        try:
            if not AIOHTTP_SESSION:
                AIOHTTP_SESSION = aiohttp.ClientSession()
            async with AIOHTTP_SESSION.get(args.custom_js) as resp:
                if resp.status != 200:
                    yield f"Got status {resp.status} (expected 200) when fetching custom JS."
                    return
                custom_js = await resp.text()
                if not custom_js.startswith("//! chromebot v1\n"):
                    yield "Custom JS must start with a valid Chromebot header."
                    return
        except Exception:
            yield "An error occured when retrieving custom JS."
            return
    result = validators.url(args.url, strict_query = False, private = False)
    if result is not True:
        yield "Failed to validate your URL. Cowardly bailing out."
        return
    ent = await QUEUE.new(
        args.url,
        "brozzler",
        user.nick,
        explanation = args.explanation,
        metadata = {"user_agent": args.user_agent, "custom_js": custom_js},
    )
    yield f"Queued {args.url} for Brozzler-based archival. You will be notified when it finishes. Use !status {ent.id} for details."
brozzle.help = brozzle.parser.format_usage().strip() + brozzle.help

async def generate_status_message(job: str):
    ent = await QUEUE.get(job)
    if not ent:
        return f"No job with ID {repr(job)} could be found. Note: Items cannot currently be looked up by URL."
    return f"Job {ent.id} ({repr(ent.item)}) has status {ent.status.upper()} and was queued at {ent.queued_at.isoformat(timespec='seconds')}. Explanation: {ent.explanation}"

@bot.command("!status")
async def status(self: Bot, user: User, ran, *jobs):
    """
    Usage: !status [IDENTS...]
    If IDENTS are provided, retrieve the status of one or more jobs.
    Otherwise, retrieve the queue status.
    """
    if jobs:
        for job in jobs:
            yield await generate_status_message(job)
    else:
        c = await QUEUE.counts()
        if not c.counts:
            yield "There aren't any queued, running, or stashed items."
        for pipeline_type, counts in c.counts.items():
            yield f"Status for {pipeline_type.upper()}: {counts['todo']} pending items, {counts['claimed']} items in progress, {counts['stashed']} items stashed away."
        if limbo := c.limbo:
            yield f"{limbo} items are possibly in limbo and may need admin intervention."

@bot.command("!limbo")
async def limbo(self: Bot, user: User, ran):
    jobs = await QUEUE.get_limbo()
    yield f"Jobs in limbo: {repr(jobs)}"

@bot.command({"!w", "!whereis"})
async def whereis(self: Bot, user: User, ran, id: str):
    job = await QUEUE.get(id)
    if not job:
        yield f"Job {id} does not exist."
    elif job.status != Status.CLAIMED:
        yield f"Job {job.id} is not currently claimed."
    else:
        yield f"Job {job.id} is currently claimed by pipeline {job.pipeline_type}."

@bot.command({"!explain", "!e"})
async def explain(self: Bot, user: User, ran, id: str, *reason):
    job = await QUEUE.get(id)
    if not job:
        yield f"Job {id} does not exist."
    else:
        r = " ".join(reason)
        nj = await QUEUE.change_explanation(job, r)
        yield f"Reason for {job.id} set to {nj.explanation!r}."

@bot.command("!!reclaim", required_modes="@")
async def abandon(self: Bot, user: User, ran, id: str, *reason):
    """
    Usage: !!reclaim <IDENT> <REASON...>
    Fails a job, allowing it to potentially be retried, without telling the pipeline.
    If the pipeline is still working on the job, this may result in duplicate items.
    """
    item = await QUEUE.get(id)
    if not item:
        yield f"Job {id} does not exist."
        return
    if item.status != Status.CLAIMED:
        yield f"Job {id} is not claimed."
        return
    if not reason:
        yield "You must provide a reason."
        return
    reason = f"Manual reclaim by {user.nick}: {' '.join(reason)}"
    yield f"Explanation: {reason!r}"
    new_item = await QUEUE.fail(item, reason)
    if new_item.status == Status.ERROR:
        yield f"Max tries reached for {new_item.id}, it has been moved to ERROR."
    elif new_item.status == Status.TODO:
        yield f"{new_item.id} has been moved to todo. Tries: {new_item.tries}"
    else:
        yield f"Unexpected status {new_item.status} for item {new_item.id}. This is probably bad."

@bot.command("!!dripfeed", required_modes = "@")
async def dripfeed(self: Bot, user: User, ran: str, stash: str, raw_concurrency: str):
    """
    Usage: !!dripfeed <STASH> <CONCURRENCY>
    Makes a stash dripfeed into todo. Chromebot will periodically move items from the stash into the queue, in regular dequeuing order. No more than CONCURRENCY items from the stash will be in the queue at once.
    The exact rate at which Chromebot dripfeeds items is undefined, but the concurrency limit will always be respected.

    Set CONCURRENCY to 0 to disable dripfeeding. Items that have already been dripfed will not be affected.
    """
    try:
        concurrency: int = int(raw_concurrency)
    except ValueError:
        yield "Invalid integer."
        return
    await QUEUE.set_dripfeed_behaviour(stash, concurrency)
    if concurrency > 0:
        yield f"Enabled dripfeeding of {stash} with a rate of {concurrency} concurrent."
    else:
        yield f"Disabled dripfeeding of {stash}."

@bot.command("!help")
async def help(self: Bot, user: User, ran, command = None):
    """
    Seriously?
    """
    if command:
        if not command.startswith("!"):
            command = f"!{command}"
        runner = self.lookup_command(command)
        if runner and runner.help:
            for line in runner.help.split("\n"):
                if line := line.strip(" \n\t"):
                    yield line
        else:
            yield f"{command} either does not exist or is undocumented. (To view an operator command, you must use !!help.)"
    else:
        help_msg = \
            f"""
            (To view help for operator commands, use !!help.)
            List of commands:
            - {Format.BOLD}!help <COMMAND>{Format.BOLD}: retrieve detailed information about a command
            - {Format.BOLD}!b <URL>{Format.BOLD} {Colour.make_colour(Colour.GREY)}(requires +v){Format.RESET}: brozzle a URL; use !b --help for more details
            - {Format.BOLD}!whereis <JOB> {Format.BOLD} {Colour.make_colour(Colour.GREY)}(alias w){Format.RESET}: find what pipeline a job is running on
            - {Format.BOLD}!status [JOBS...]{Format.BOLD}: gets status of all queues, or status of (a) particular job(s)
            - {Format.BOLD}!limbo{Format.BOLD}: retrieves all jobs in limbo
            """
        for line in help_msg.split("\n"):
            if line := line.strip(" \n\t"):
                yield line

@bot.command("!!help")
async def danger_help(self: Bot, user: User, ran, command = None):
    if command:
        while not command.startswith("!!"):
            command = f"!{command}"
        runner = self.lookup_command(command)
        if runner and runner.help:
            for line in runner.help.split("\n"):
                if line := line.strip(" \n\t"):
                    yield line
        else:
            yield f"{command} either does not exist or is undocumented. (To view a regular command, use !help.)"
    else:
        help_msg = \
            f"""
            (To view help for regular commands, use !help.)
            {Format.BOLD}Some commands in this list are dangerous! Use with caution.
            List of operator commands:
            {Format.BOLD}!!help <COMMAND>{Format.RESET}: retrieve detailed information about operator command
            {Format.BOLD}!!reclaim <IDENT> <REASON...> {Format.RESET}: fail item without notifying client
            """
        for line in help_msg.split("\n"):
            line = line.strip(" \n\t")
            if line:
                yield line

asyncio.run(bot.run_forever())

