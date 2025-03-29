# This file is called as a subprocess from app.py.
# It takes one argument in argv, which is a file descriptor for outputting control data.

### HEY YOU! Yeah, you!
### If you are making any change to the client, please
### update the version in meta.py.
### No two versions in prod should have the same version number.

import base64
import typing, urllib.request, json, shutil, sys, traceback

from shared import DEBUG, VERSION, PROXY_URL, Job, Result
from result import *

import logging
logger = logging.getLogger("mnbot")
if DEBUG:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)
del logging

import brozzler
import requests

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
        self.screenshot = screenshot
        self.thumbnail = thumbnail
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

    def _proxy_url(self, url: str, headers: dict):
        proxies = {
            "http": f"http://{PROXY_URL}",
            "https": f"https://{PROXY_URL}"
        }
        logger.debug(f"fetching {url}")
        return requests.get(
            url,
            proxies = proxies,
            headers = headers,
            verify = False
        )

    def _best_effort_proxy_url(self, url: str, headers: dict):
        try:
            return self._proxy_url(url, headers)
        except Exception:
            logger.warning(f"failed to proxy URL; stifling issue", exc_info = True)
            return None

    def _brozzle(self, browser: brozzler.Browser, job: Job) -> Result:
        assert not browser.is_running()
        extra_headers = {
            "Warcprox-Meta": json.dumps({
                "warc-prefix": job.warc_prefix,
                #"dedup-buckets": {"successful_jobs": "ro", dedup_bucket: "rw"}
                "dedup-buckets": {job.dedup_bucket: "rw"},
                "stats": {"buckets": [job.stats_bucket]}
            }),
        }
        logger.debug("starting browser")
        browser.start(
            proxy = "http://" + PROXY_URL,
            cookie_db = job.cookie_jar
        )

        # Shim for _handle_message so we can log responses
        # on_request and on_response don't allow us to see errors or the *final* response size
        url_log: dict[str, Chain] = {}
        logger.debug("shimming brozzler")
        previous_message_handler = browser.websock_thread._handle_message
        def message_handler(websock, json_message: str):
            # brozzler gets priority so an exception can't lose messages
            previous_message_handler(websock, json_message)
            message = json.loads(json_message)
            if "method" in message:
                method = message['method']
                params = message['params']
                if method == "Network.requestWillBeSent":
                    request_id = params['requestId']
                    if request_id not in url_log:
                        url_log[request_id] = Chain([], request_id)
                    if redirect_response := params.get("redirectResponse"):
                        url_log[request_id].chain[-1].response = Response.from_dict(redirect_response)
                    request = Request.from_params(params)
                    url_log[request_id].chain.append(Pair(request, None))
                elif method == "Network.responseReceived":
                    request_id = params['requestId']
                    response = Response.from_params(params)
                    url_log[request_id].chain[-1].response = response
                elif method == "Network.dataReceived":
                    request_id = params['requestId']
                    url_log[request_id].chain[-1].response.length += params['dataLength']
                elif method == "Network.loadingFinished":
                    # Future: Submit log to websocket
                    pass
                elif method == "Network.loadingFailed":
                    request_id = params['requestId']
                    error = Error(text = params['errorText'])
                    url_log[request_id].chain[-1].response = error
        browser.websock_thread._handle_message = message_handler

        logger.debug("getting user agent")
        ua = self._run_cdp_command(
            browser,
            "Runtime.evaluate",
            {"expression": "navigator.userAgent", "returnByValue": True}
        )['result']['result']['value']
        logger.debug(f"got user agent {ua}")
        # pretend we're not headless
        ua = ua.replace("HeadlessChrome", "Chrome")
        # pretend to be Windows, as brozzler's stealth JS does (otherwise it's inconsistent)
        ua = ua.replace("(X11; Linux x86_64)", "(Windows NT 10.0; Win64; x64)")
        if not job.stealth_ua:
            # add mnbot link
            ua += f" (mnbot {VERSION}; +{job.mnbot_info_url})"
        logger.debug(f"using updated user agent {ua}")

        logger.debug("writing item info")
        self._write_warcprox_record(
            "metadata:mnbot-job-metadata",
            "application/json",
            json.dumps({
                "job": job.full_job,
                "version": VERSION
            }).encode(),
            job.warc_prefix
        )
        canon_url = str(brozzler.urlcanon.semantic(job.url))

        def on_screenshot(data):
            self._on_screenshot(data, canon_url, job.warc_prefix)

        # Shamelessly stolen from brozzler's Worker._browse_page.
        # Apparently service workers don't get added to the right WARC:
        # https://github.com/internetarchive/brozzler/issues/140
        # This fetches them with requests to work around it.
        already_fetched = set()
        def _on_service_worker_version_updated(chrome_msg):
            url = chrome_msg.get("params", {}).get("versions", [{}])[0].get("scriptURL")
            if url and url not in already_fetched:
                logger.info(f"fetching service worker script {url}")
                self._best_effort_proxy_url(url, extra_headers)
                already_fetched.add(url)

        logger.debug("browsing page")
        final_url, outlinks = browser.browse_page(
            page_url = job.url,
            user_agent = ua,
            skip_youtube_dl = True,
            # We do these two things manually so they happen after custom_js
            skip_extract_outlinks = True,
            skip_visit_hashtags = True,
            on_screenshot = on_screenshot,
            on_service_worker_version_updated = _on_service_worker_version_updated,
            stealth = True,
            extra_headers = extra_headers
        )
        assert len(outlinks) == 0, "Brozzler didn't listen to us :["
        status_code: int = browser.websock_thread.page_status

        # This is different than brozzler's built-in behaviour_dir thingy because
        # we actually save the output.
        custom_js_result = None
        if job.custom_js:
            logger.debug("running custom behaviour")
            message = self._run_cdp_command(
                browser = browser,
                method = "Runtime.evaluate",
                params = {
                    "expression": job.custom_js,
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
        self._write_warcprox_record("rendered-dom:" + canon_url, "text/html", outer_html.encode(), job.warc_prefix)

        r = Result(
            final_url = final_url,
            outlinks = list(outlinks),
            custom_js = custom_js_result,
            status_code = status_code,
            requisites = url_log
        )
        logger.debug("writing job result data")
        self._write_warcprox_record(
            "metadata:mnbot-job-result",
            "application/json",
            json.dumps({
                "result": r.dict()
            }).encode(),
            job.warc_prefix
        )

        return r

    def run_job(self, job: Job) -> Result:
        logger.debug(f"spun up thread for job {job.full_job['id']}")
        browser = self.pool.acquire()
        try:
            return self._brozzle(browser, job)
        finally:
            browser.stop()
            self.pool.release(browser)

if __name__ == "__main__":
    with open(int(sys.argv[1]), "w") as control:
        def write_message(type, payload):
            control.write(json.dumps({"type": type, "payload": payload}) + "\n")

        # todo: remove the pool entirely, just do one browser
        pool = Brozzler(1)
        job_data = json.loads(sys.stdin.readline())
        job = Job(**job_data)
        try:
            result = pool.run_job(job)
            write_message("final_url", result.final_url)
            write_message("outlinks", result.outlinks)
            write_message("requisites", [dataclasses.asdict(v) for v in result.requisites.values()])
            write_message("status_code", result.status_code)
            write_message("screenshot", {
                "full": base64.b85encode(pool.screenshot).decode(),
                "thumb": base64.b85encode(pool.thumbnail).decode()
            })
            if jsr := result.custom_js:
                write_message("custom_js", result.custom_js)
                if jsr['status'] != "success":
                    raise RuntimeError("Custom JS didn't succeed")
            if result.status_code >= 400:
                raise RuntimeError(f"Bad HTTP status code {result.status_code}")
            elif not result.final_url.startswith("http"):
                raise RuntimeError(f"Bad final_url {result.final_url}")
        except Exception:
            write_message("error", "Caught exception!\n" + traceback.format_exc())
