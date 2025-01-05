import asyncio, dataclasses, time, typing, urllib.request, json, shutil, os

import logging
logger = logging.getLogger("chromebot")
del logging

from meta import VERSION
from rethinkdb import r

if typing.TYPE_CHECKING:
    from tracker import Websocket

import brozzler

PROXY_URL = "warcprox:8000"

######### INITIALISATION #########

# Wait for warcprox to start
while True:
    try:
        with urllib.request.urlopen("http://" + PROXY_URL.rstrip("/") + "/status") as conn:
            if conn.getcode() == 200:
                break
            else:
                logger.info("warcprox isn't healthy yet, sleeping")
                time.sleep(5)
    except Exception:
        logger.info("no warcprox yet, sleeping")
        time.sleep(5)

# Convenience function to get the dedup DB
def _dedup_db():
    return r.db("cb-warcprox").table("dedup")

# Convenience function to get the stats DB
def _stats_db():
    return r.db("cb-warcprox").table("stats")

# Add our index
def _setup_db():
    conn = r.connect(host = "rethinkdb")
    indexes = _dedup_db().index_list().run(conn)
    #if "chromebot-bucket" not in indexes:
    #    _dedup_db().index_create("chromebot-bucket", lambda row : row['key'].split("|", 1)).run(conn)
    if "chromebot-date" not in indexes:
        _dedup_db().index_create("chromebot-date", r.iso8601(r.row['date'])).run(conn)
    conn.close()
_setup_db()
r.set_loop_type("asyncio")

######### THE ACTUALLY IMPORTANT STUFF #########

@dataclasses.dataclass
class Result:
    final_url: str
    outlinks: list
    custom_js_result: typing.Optional[dict]
    status_code: int

    def dict(self) -> dict[str, typing.Any]:
        return {
            "final_url": self.final_url,
            "outlinks": self.outlinks,
            "custom_js_result": self.custom_js_result
            # Don't include status code because that's already in the WARC
        }

def thumb_jpeg(full_jpeg):
    # This really should be a static method...
    return brozzler.BrozzlerWorker.thumb_jpeg(None, full_jpeg)

class Brozzler:
    def __init__(self, browsers: int):
        self.pool = brozzler.BrowserPool(
            browsers,
            chrome_exe = shutil.which("chromium"),
            ignore_cert_errors = True
        )

    def _write_warcprox_record(self, url: str, content_type: str, payload, warc_prefix):
        logger.debug(f"writing {url} ({content_type}) to WARC")
        headers = {
            "Content-Type": content_type,
            "WARC-Type": "resource",
            "Host": PROXY_URL,
            "Warcprox-Meta": json.dumps({"warc-prefix": warc_prefix})
        }
        request = urllib.request.Request(
            url,
            method = "WARCPROX_WRITE_RECORD",
            headers = headers,
            data = payload
        )
        request.type = "http"
        request.set_proxy(PROXY_URL, "http")

        with urllib.request.urlopen(request, timeout=600) as response:
            if response.getcode() != 204:
                raise RuntimeError("Bad status code from Warcprox")

    def _on_screenshot(self, screenshot, url: str, warc_prefix: str):
        # Inspired by Brozzler's implementation of this. Brozzler is also Apache2.0-licenced
        logger.debug("writing screenshot")
        thumbnail = thumb_jpeg(screenshot)
        self._write_warcprox_record(
            url = "screenshot:" + url,
            content_type = "image/jpeg",
            payload = screenshot,
            warc_prefix = warc_prefix
        )
        self._write_warcprox_record(
            url = "thumbnail:" + url,
            content_type = "image/jpeg",
            payload = thumbnail,
            warc_prefix = warc_prefix
        )

    def _run_cdp_command(self, browser: brozzler.Browser, method: str, params: dict = {}) -> typing.Any:
        # Abuse brozzler's innards a bit to run a custom CDP command.
        # If this breaks, I was never here.
        # TODO: Burn with fire.
        logger.debug(f"running CDP command {method}")
        assert browser.websock_thread
        browser.websock_thread.expect_result(browser._command_id.peek())
        msg_id = browser.send_to_chrome(
            method = method,
            params = params
        )
        logger.debug("waiting for response")
        browser._wait_for(
            lambda : browser.websock_thread.received_result(msg_id),
            timeout = 60
        )
        message = browser.websock_thread.pop_result(msg_id)
        m = repr(message)
        if len(m) < 1024:
            logger.debug(f"received response: {message}")
        else:
            logger.debug(f"received response (too long for logs)")
        return message

    def _brozzle(self, browser, full_job: dict, url: str, warc_prefix: str, dedup_bucket: str, stats_bucket: str, user_agent: typing.Optional[str], custom_js: typing.Optional[str], cookie_jar = None) -> Result:
        assert not browser.is_running()
        logger.debug("writing item info")
        self._write_warcprox_record(
            "metadata:chromebot-job-metadata",
            "application/json",
            json.dumps({
                "job": full_job,
                "version": VERSION
            }).encode(),
            warc_prefix
        )
        logger.debug("starting browser")
        logger.critical(os.environ['BROZZLER_EXTRA_CHROME_ARGS'])
        browser.start(
            proxy = "http://" + PROXY_URL,
            cookie_db = cookie_jar
        )
        canon_url = str(brozzler.urlcanon.semantic(url))

        def on_screenshot(data):
            self._on_screenshot(data, canon_url, warc_prefix)

        logger.debug("browsing page")
        final_url, outlinks = browser.browse_page(
            page_url = url,
            user_agent = user_agent,
            skip_youtube_dl = True,
            # We do these two things manually so they happen after custom_js
            skip_extract_outlinks = True,
            skip_visit_hashtags = True,
            on_screenshot = on_screenshot,
            stealth = True,
            extra_headers = {
                "Warcprox-Meta": json.dumps({
                    "warc-prefix": warc_prefix,
                    #"dedup-buckets": {"successful_jobs": "ro", dedup_bucket: "rw"}
                    "dedup-buckets": {dedup_bucket: "rw"},
                    "stats": {"buckets": [stats_bucket]}
                }),
            }
        )
        assert len(outlinks) == 0, "Brozzler didn't listen to us :["
        status_code: int = browser.websock_thread.page_status

        # This is different than brozzler's built-in behaviour_dir thingy because
        # we actually save the output.
        custom_js_result = None
        if custom_js:
            logger.debug("running custom behaviour")
            message = self._run_cdp_command(
                browser = browser,
                method = "Runtime.evaluate",
                params = {
                    "expression": custom_js,
                    # Allow let redeclaration and await
                    # Let redeclaration isn't really necessary, but top-level await is nice.
                    "replMode": True,
                    # Makes it actually return the value
                    "returnByValue": True,
                }
            )
            try:
                custom_js_result = {
                    "status": "success",
                    "remoteObject": message['result']['result'],
                    "exceptionDetails": message['result'].get("exceptionDetails")
                }
                if custom_js_result.get("exceptionDetails") is not None:
                    custom_js_result['status'] = "exception"
            except KeyError:
                logger.error(f"unreadable response: {message}")
                custom_js_result = {
                    "status": "unknown",
                    "fullResult": message.get("result")
                }

        logger.debug("extracting outlinks")
        outlinks = browser.extract_outlinks()
        logger.debug("visiting anchors")
        browser.visit_hashtags(final_url, [], outlinks)

        # Dump the DOM
        # First get the root node ID using DOM.getDocument (which gets the root node).
        logger.debug("getting root node ID")
        root_node_id = self._run_cdp_command(browser, "DOM.getDocument")['result']['root']['nodeId']
        # Now get the outer HTML of the root node.
        logger.debug(f"getting outer HTML for node {root_node_id}")
        outer_html = self._run_cdp_command(browser, "DOM.getOuterHTML", {"nodeId": root_node_id})['result']['outerHTML']
        # And write it to the WARC.
        logger.debug("writing outer HTML to WARC")
        self._write_warcprox_record("rendered-dom:" + canon_url, "text/html", outer_html.encode(), warc_prefix)

        r = Result(
            final_url = final_url,
            outlinks = list(outlinks),
            custom_js_result = custom_js_result,
            status_code = status_code
        )
        logger.debug("writing job result data")
        self._write_warcprox_record(
            "metadata:chromebot-job-result",
            "application/json",
            json.dumps({
                "result": r.dict()
            }).encode(),
            warc_prefix
        )

        return r

    def _run_job_target(self, full_job: dict, url: str, warc_prefix: str, dedup_bucket: str, stats_bucket: str, user_agent: typing.Optional[str], custom_js: typing.Optional[str], cookie_jar = None) -> Result:
        logger.debug(f"spun up thread for job {full_job['id']}")
        browser = self.pool.acquire()
        try:
            return self._brozzle(browser, full_job, url, warc_prefix, dedup_bucket, stats_bucket, user_agent, custom_js, cookie_jar)
        finally:
            browser.stop()
            self.pool.release(browser)

    async def run_job(self, ws: "Websocket", full_job: dict, url: str, warc_prefix: str, user_agent: typing.Optional[str], custom_js: typing.Optional[str]):
        tries = full_job['tries']
        id = full_job['id']
        #dedup_bucket = f"dedup-{id}-{tries}"
        dedup_bucket = ""
        stats_bucket = f"stats-{id}-{tries}"
        result = await asyncio.to_thread(
            self._run_job_target,
            full_job = full_job,
            url = url,
            warc_prefix = warc_prefix,
            dedup_bucket = dedup_bucket,
            stats_bucket = stats_bucket,
            user_agent = user_agent,
            custom_js = custom_js,
        )
        await ws.store_result(id, "status_code", tries, result.status_code)
        await ws.store_result(id, "outlinks", tries, result.outlinks)
        await ws.store_result(id, "final_url", tries, result.final_url)
        if result.status_code >= 400:
            raise RuntimeError(f"Bad status code {result.status_code}")
        if jsr := result.custom_js_result:
            await ws.store_result(id, "custom_js", tries, jsr)
            if jsr['status'] != "success":
                raise RuntimeError("Custom JS didn't succeed")
        return dedup_bucket, stats_bucket

async def warcprox_cleanup():
    conn = None
    try:
        conn = await r.connect(host = "rethinkdb")
        # We don't currently clean up the stats bucket as warcprox does it in batches, so when
        # this function runs, warcprox is going to re-add it in a few seconds anyway.
        logger.debug("deleting old dedup records")
        res = await (
            _dedup_db()
            # Deletes records more than 7 days old, to prevent the database from blowing up
            # and to make sure that corrupt records don't forever cause a URL to be lost
            .between(r.minval, r.now() - 7*24*3600, index = "chromebot-date")
            .delete(durability = "soft")
            .run(conn)
        )
        logger.debug(f"queued {res['deleted']} old dedup records for deletion")
    except Exception:
        logger.exception(f"failed to clean up old records")
    finally:
        try:
            if conn:
                await conn.close()
        except Exception:
            pass
