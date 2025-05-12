import os
import asyncio

import aiohttp
import validators

from bot2h import Bot, Colour, Format, User
from rue import Queue, Status

H2IBOT_GET_URL = os.environ['H2IBOT_GET_URL']
H2IBOT_POST_URL = os.environ['H2IBOT_POST_URL']
TRACKER_BASE_URL = os.environ['TRACKER_BASE_URL'].rstrip("/")
DOCUMENTATION_URL = os.environ['DOCUMENTATION_URL']

def item_url(id: str | None):
    if not id:
        return "<N/A>"
    return f"{TRACKER_BASE_URL}/item/{id}"

bot = Bot(H2IBOT_GET_URL, H2IBOT_POST_URL, max_coros = 1)

QUEUE = Queue("mnbot")
asyncio.run(QUEUE.check())
print("setup complete")

AIOHTTP_SESSION = None

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

@bot.add_argument(
    "--user-agent", "-u",
    choices = ("default", "stealth", "curl", "archivebot", "minimal", "googlebot1", "googlebot"),
    default = "default"
)
@bot.add_argument("--explanation", "--explain", "-e")
@bot.add_argument("--custom-js")
@bot.add_argument("url")
@bot.argparse("!brozzle")
@bot.command({"!b", "!brozzle"}, required_modes="+@")
async def brozzle(self: Bot, user: User, ran, args):
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
                if not custom_js.startswith("//! mnbot v1\n"):
                    yield "Custom JS must start with a valid mnbot header."
                    return
        except Exception:
            yield "An error occured when retrieving custom JS."
            return
    result = validators.url(args.url, strict_query = False, private = False)
    if result is not True:
        yield "Failed to validate your URL. Cowardly bailing out."
        return
    ua = PRESET_USER_AGENTS.get(args.user_agent, args.user_agent)
    if ua := PRESET_USER_AGENTS.get(args.user_agent):
        ua = "$" + ua
    else:
        ua = args.user_agent
    ent = await QUEUE.new(
        args.url,
        "brozzler",
        user.nick,
        explanation = args.explanation,
        metadata = {"ua": ua, "custom_js": custom_js},
    )
    yield f"Queued {args.url} for Brozzler-based archival. You will be notified when it finishes. Use !status {ent.id} or check {item_url(ent.id)} for details."

async def generate_status_message(job: str):
    ent = await QUEUE.get(job)
    if not ent:
        return f"No job with ID {repr(job)} could be found. Note: Items cannot currently be looked up by URL."
    return f"Job {ent.id} ({repr(ent.item)}) has status {ent.status.upper()} and was queued at {ent.queued_at.isoformat(timespec='seconds')}. See {item_url(ent.id)} for more information. Explanation: {ent.explanation}"

@bot.command("!status")
async def status(self: Bot, user: User, ran, *jobs):
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
    new_item = await QUEUE.fail(item, reason, item.current_attempt(), is_poke = True)
    if new_item.status == Status.ERROR:
        yield f"Max tries reached for {new_item.id}, it has been moved to ERROR."
    elif new_item.status == Status.TODO:
        yield f"{new_item.id} has been moved to todo. Tries: {len(new_item.attempts)}"
    else:
        yield f"Unexpected status {new_item.status} for item {new_item.id}. This is probably bad."

@bot.command("!!dripfeed", required_modes = "@")
async def dripfeed(self: Bot, user: User, ran: str, stash: str, raw_concurrency: str):
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
    yield f"Documentation can be found at {DOCUMENTATION_URL}."

asyncio.run(bot.run_forever())

