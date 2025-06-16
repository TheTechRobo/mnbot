# This file is called as a subprocess from app.py.
# It takes one argument in argv, which is a file descriptor for outputting control data.

############################# NOTE! ###############################
### If you are making any change to the client, please          ###
### update the version in shared.py.                            ###
### No two versions in prod should have the same version number.###
############################ THANKS! ##############################

import base64
import threading
import time
import typing, urllib.request, json, shutil, sys, traceback

from shared import DEBUG, VERSION, PROXY_URL, BadStatusCode, Job, MnError, Result, Timeout
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
import urlcanon

def thumb_jpeg(full_jpeg):
    # This really should be a static method...
    return brozzler.BrozzlerWorker.thumb_jpeg(None, full_jpeg)

class Brozzler:
    def __init__(self, job: Job):
        self.chrome_exe = shutil.which("chromium")
        if not self.chrome_exe:
            raise RuntimeError("Could not find a Chromium executable!")
        self.pool = brozzler.BrowserPool(
            1,
            chrome_exe = self.chrome_exe,
            ignore_cert_errors = True
        )
        self.browser = self.pool.acquire()
        self.browser.start(
            proxy = "http://" + PROXY_URL,
            cookie_db = job.cookie_jar,
            headless = False,
        )
        self.job = job
        self.canon_url = str(urlcanon.semantic(job.url))
        self.extra_headers = {
            "Warcprox-Meta": json.dumps({
                "warc-prefix": job.warc_prefix,
                #"dedup-buckets": {"successful_jobs": "ro", dedup_bucket: "rw"}
                "dedup-buckets": {job.dedup_bucket: "rw"},
                "stats": {"buckets": [job.stats_bucket]}
            }),
        }
        self.websock_thread_lock = threading.Lock()
        self.active_connections = 0
        self.last_network_activity = time.time()
        self.url_log: dict[str, Chain] = {}

        # Shim for _handle_message so we can log responses
        # on_request and on_response don't allow us to see errors or the *final* response size
        # Also allows us to create a sort of networkIdle event
        logger.debug("shimming brozzler")
        self.previous_message_handler = self.browser.websock_thread._handle_message
        self.browser.websock_thread._handle_message = self._message_handler

        self.already_fetched = set()

        self.screenshot, self.thumbnail = None, None

    @staticmethod
    def _wait_for(check: typing.Callable, timeout: int | None, desc: str):
        logger.debug(f"waiting up to {timeout}s for {desc}")
        start_time = time.time()
        while not check():
            if timeout is not None:
                diff = time.time() - start_time
                if diff >= timeout:
                    raise Timeout(
                        f"timed out after {round(diff, 1)}s waiting for {desc}"
                    )
            time.sleep(0.5)

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

    def _on_screenshot(self, screenshot):
        # Inspired by Brozzler's implementation of this. Brozzler is also Apache2.0-licenced
        logger.debug("processing screenshot")
        if not screenshot:
            logger.error("got empty screenshot from chrome, returning")
            return
        self.screenshot = screenshot
        if self.status_code < 400:
            # Avoids writing broken screenshots to WARC
            # (including warcprox's error pages!)
            self._write_warcprox_record(
                url = "screenshot:" + self.canon_url,
                content_type = "image/jpeg",
                payload = screenshot,
                warc_prefix = self.job.warc_prefix
            )

        try:
            self.thumbnail = thumb_jpeg(screenshot)
        except Exception:
            logger.exception("failed to convert screenshot to thumbnail")
        else:
            if self.status_code < 400:
                self._write_warcprox_record(
                    url = "thumbnail:" + self.canon_url,
                    content_type = "image/jpeg",
                    payload = self.thumbnail,
                    warc_prefix = self.job.warc_prefix
                )

    def _run_cdp_command(self, method: str, params: dict | None = None) -> typing.Any:
        # Abuse brozzler's innards a bit to run a custom CDP command.
        # If this breaks, I was never here.
        # TODO: Burn with fire.
        params = params or {}
        logger.debug(f"running CDP command {method}")
        assert self.browser.websock_thread
        self.browser.websock_thread.expect_result(self.browser._command_id.peek())
        msg_id = self.browser.send_to_chrome(
            method = method,
            params = params
        )
        logger.debug("waiting for response")
        self._wait_for(
            lambda : self.browser.websock_thread.received_result(msg_id),
            45,
            "CDP result"
        )
        message = self.browser.websock_thread.pop_result(msg_id)
        m = repr(message)
        if len(m) < 1024:
            logger.debug("received response: %s", message)
        else:
            logger.debug("received response (too long for logs)")
        return message

    @staticmethod
    def _proxy_url(url: str, headers: dict):
        """
        Runs a URL through warcprox with requests.
        """
        proxies = {
            "http": f"http://{PROXY_URL}",
            "https": f"http://{PROXY_URL}"
        }
        logger.debug(f"fetching {url}")
        return requests.get(
            url,
            proxies = proxies,
            headers = headers,
            verify = False,
            timeout = 15
        )

    @classmethod
    def _best_effort_proxy_url(cls, url: str, headers: dict):
        """
        Runs _proxy_url ignoring exceptions.
        """
        try:
            return cls._proxy_url(url, headers)
        except Exception:
            logger.warning("failed to proxy URL; stifling issue", exc_info = True)
            return None

    def _message_handler(self, websock, json_message: str):
        """
        Shim for browser.websock_thread._handle_message.

        Keeps track of network requests for logging and checking for idle.
        """
        # brozzler gets priority so an exception can't lose messages
        self.previous_message_handler(websock, json_message)
        message = json.loads(json_message)
        with self.websock_thread_lock:
            if "method" in message:
                method = message['method']
                params = message['params']
                if method == "Network.requestWillBeSent":
                    self.active_connections += 1
                    request_id = params['requestId']
                    if request_id not in self.url_log:
                        self.url_log[request_id] = Chain([], request_id)
                    if redirect_response := params.get("redirectResponse"):
                        self.url_log[request_id].chain[-1].response = Response.from_dict(redirect_response)
                    request = Request.from_params(params)
                    self.url_log[request_id].chain.append(Pair(request, None))
                    self.last_network_activity = time.time()
                elif method == "Network.responseReceived":
                    request_id = params['requestId']
                    response = Response.from_params(params)
                    self.url_log[request_id].chain[-1].response = response
                    self.last_network_activity = time.time()
                elif method == "Network.dataReceived":
                    request_id = params['requestId']
                    self.url_log[request_id].chain[-1].response.length += params['dataLength']
                    self.last_network_activity = time.time()
                elif method == "Network.loadingFinished":
                    # Future: Submit log to websocket
                    self.active_connections -= 1
                    self.last_network_activity = time.time()
                    pass
                elif method == "Network.loadingFailed":
                    self.active_connections -= 1
                    self.last_network_activity = time.time()
                    request_id = params['requestId']
                    error = Error(text = params['errorText'])
                    self.url_log[request_id].chain[-1].response = error
            if self.active_connections < 0:
                self.active_connections = 0

    # Shamelessly stolen from brozzler's Worker._browse_page.
    # Apparently service workers don't get added to the right WARC:
    # https://github.com/internetarchive/brozzler/issues/140
    # This fetches them with requests to work around it.
    def _on_service_worker_version_updated(self, chrome_msg):
        url = chrome_msg.get("params", {}).get("versions", [{}])[0].get("scriptURL")
        if url and url not in self.already_fetched:
            logger.info(f"fetching service worker script {url}")
            self._best_effort_proxy_url(url, self.extra_headers)
            self.already_fetched.add(url)

    def _create_user_agent(self, version: dict) -> str:
        """
        Decides on a user agent for the job.
        """
        match self.job.ua:
            case "default" | "stealth":
                logger.debug("getting user agent")
                ua = self._run_cdp_command(
                    "Runtime.evaluate",
                    {"expression": "navigator.userAgent", "returnByValue": True}
                )['result']['result']['value']
                logger.debug(f"got user agent {ua}")
                # pretend we're not headless
                ua = ua.replace("HeadlessChrome", "Chrome")
                # pretend to be Windows, as brozzler's stealth JS does (otherwise it's inconsistent)
                ua = ua.replace("(X11; Linux x86_64)", "(Windows NT 10.0; Win64; x64)")
                if self.job.ua != "stealth":
                    # add mnbot link
                    ua += f" (mnbot {VERSION}; +{self.job.mnbot_info_url})"
                logger.info(f"using updated user agent {ua}")
            case "googlebot":
                chrome_version = version['product'] # Chrome/W.X.Y.Z
                ua = (
                    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; "
                    "compatible; Googlebot/2.1; +http://www.google.com/bot.html) " +
                    chrome_version + "Safari/537.36"
                )
                logger.info(f"using googlebot user agent {ua}")
            case "minimal":
                ua = f"mnbot {VERSION} (+{self.job.mnbot_info_url})"
                logger.info(f"using minimal user agent {ua}")
            case _:
                # Server should send a string starting with $ to denote a literal user agent
                if not self.job.ua.startswith("$"):
                    raise RuntimeError("Server gave us an invalid user agent. Is the pipeline out of date?")
                ua = self.job.ua[1:]
                logger.info(f"using literal user agent {ua}")

        return ua

    def _run_custom_js(self):
        logger.debug("running custom behaviour")
        message = self._run_cdp_command(
            method = "Runtime.evaluate",
            params = {
                "expression": self.job.custom_js,
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
        return custom_js_result

    def _browser_version(self):
        logger.debug("getting browser version")
        version = self._run_cdp_command(
            "Browser.getVersion"
        )['result']
        version['defaultUserAgent'] = version['userAgent']
        del version['userAgent']
        logger.debug("got browser version %s", version)
        return version

    def _wait_for_idle(self, *, idle_time: float, timeout: int):
        """
        Waits up to timeout seconds for network to have been idle for idle_time.
        """
        logger.debug("waiting for network idle")
        def is_idle():
            with self.websock_thread_lock:
                diff = time.time() - self.last_network_activity
                return self.active_connections == 0 and diff > idle_time
        self._wait_for(is_idle, timeout, "idle")
        logger.debug("got network idle!")

    def _brozzle(self) -> Result:
        assert self.browser.is_running()

        version = self._browser_version()
        ua = self._create_user_agent(version)

        # Write job info to the WARC
        logger.debug("writing item info")
        self._write_warcprox_record(
            "metadata:mnbot-job-metadata",
            "application/json",
            json.dumps({
                "job": self.job.full_job,
                "version": VERSION,
                "browser": {
                    "executable": self.chrome_exe,
                    "version": version
                }
            }).encode(),
            self.job.warc_prefix
        )

        logger.debug("browsing page")
        final_url, outlinks = self.browser.browse_page(
            page_url = self.job.url,
            user_agent = ua,
            skip_youtube_dl = True,
            # We do these two things ourselves so they happen after custom_js
            skip_extract_outlinks = True,
            skip_visit_hashtags = True,
            on_screenshot = None,
            on_service_worker_version_updated = self._on_service_worker_version_updated,
            stealth = True,
            extra_headers = self.extra_headers,
            # Allow behaviour to run for up to 3 minutes
            behavior_timeout = 180,
        )
        assert len(outlinks) == 0, "Brozzler didn't listen to us :["
        self.status_code: int = self.browser.websock_thread.page_status
        try:
            self._wait_for_idle(idle_time = 3, timeout = 10)
        except Timeout:
            logger.info("timed out waiting for idle; ignoring issue")

        logger.debug("taking screenshot")
        self.browser._try_screenshot(self._on_screenshot, full_page = True)
        logger.debug("took screenshot!")

        custom_js_result = None
        if self.job.custom_js:
            custom_js_result = self._run_custom_js()

        logger.debug("extracting outlinks")
        outlinks = self.browser.extract_outlinks()
        logger.debug("visiting anchors")
        self.browser.visit_hashtags(final_url, [], outlinks)

        # Dump the DOM
        # First get the root node ID using DOM.getDocument (which gets the root node).
        logger.debug("getting root node ID")
        root_node_id = self._run_cdp_command("DOM.getDocument")['result']['root']['nodeId']
        # Now get the outer HTML of the root node.
        logger.debug(f"getting outer HTML for node {root_node_id}")
        outer_html = self._run_cdp_command("DOM.getOuterHTML", {"nodeId": root_node_id})['result']['outerHTML']
        # And write it to the WARC.
        logger.debug("writing outer HTML to WARC")
        self._write_warcprox_record("rendered-dom:" + self.canon_url, "text/html", outer_html.encode(), self.job.warc_prefix)

        with self.websock_thread_lock:
            r = Result(
                final_url = final_url,
                outlinks = list(outlinks),
                custom_js = custom_js_result,
                status_code = self.status_code,
                requisites = self.url_log
            )
            logger.debug("writing job result data")
            self._write_warcprox_record(
                "metadata:mnbot-job-result",
                "application/json",
                json.dumps({
                    "result": r.dict()
                }).encode(),
                self.job.warc_prefix
            )

        return r

    def run_job(self) -> Result:
        try:
            return self._brozzle()
        finally:
            self.browser.stop()
            self.pool.release(self.browser)

def main():
    with open(int(sys.argv[1]), "w") as control:
        def write_message(type, payload):
            control.write(json.dumps({"type": type, "payload": payload}) + "\n")

        job_data = json.loads(sys.stdin.readline())
        job = Job(**job_data)
        try:
            browser = Brozzler(job)
            result = browser.run_job()
            write_message("final_url", result.final_url)
            write_message("outlinks", result.outlinks)
            write_message("requisites", [dataclasses.asdict(v) for v in result.requisites.values()])
            write_message("status_code", result.status_code)

            screenshot = browser.screenshot
            thumbnail = browser.thumbnail
            if screenshot:
                screenshot = base64.b85encode(screenshot).decode()
            if thumbnail:
                thumbnail = base64.b85encode(thumbnail).decode()
            if screenshot or thumbnail:
                write_message("screenshot", {
                    "full": screenshot,
                    "thumb": thumbnail,
                })

            if jsr := result.custom_js:
                write_message("custom_js", result.custom_js)
                if jsr['status'] != "success":
                    raise MnError("Custom JS didn't succeed")
            if result.status_code >= 400:
                raise BadStatusCode(result.status_code)
            elif not result.final_url.startswith("http"):
                raise MnError(f"Bad final_url {result.final_url}")
        except BadStatusCode as e:
            if e.fatal:
                write_message("fatal", f"Fatal status code {e.code} (not retrying)")
            else:
                write_message("error", f"Bad status code {e.code}")
        except Exception as e:
            if isinstance(e, MnError) and e.fatal:
                write_message("fatal", "Caught exception!\n" + traceback.format_exc())
            else:
                write_message("error", "Caught exception!\n" + traceback.format_exc())

if __name__ == "__main__":
    main()
