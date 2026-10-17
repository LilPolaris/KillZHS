"""HTTP session shared by every platform flow.

The platform pages keep one browser session per site and switch ``Origin`` /
``Referer`` per course type; :meth:`Http.ready` reproduces that switching so the
headers and cookies sent match the official pages request for request.
"""

import json
import logging
import re
import threading
import time
import urllib.request
from urllib.parse import unquote_plus

import requests
from requests.adapters import HTTPAdapter, Retry
from requests.cookies import RequestsCookieJar, create_cookie
from requests.utils import cookiejar_from_dict

from ..runtime import TaskStopped
from .crypto import VIDEO_KEY, Cipher, hike_signature
from .verification import challenge_details

log = logging.getLogger("killzhs.platform")

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "sec-ch-ua": "\" Not A;Brand\";v=\"99\", \"Chromium\";v=\"101\", \"Google Chrome\";v=\"101\"",
    "sec-ch-ua-mobile": "?0",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/100.0.4896.127 Safari/537.36",
    "sec-ch-ua-platform": "macOS",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-GB,en;q=0.9",
}
DEFAULT_TIMEOUT = 30


class PlatformError(Exception):
    pass


class CaptchaRequired(PlatformError):
    """The platform answered ``code: -12`` and wants a manual web verification."""


class NotLoggedIn(PlatformError):
    pass


def cookies_to_list(jar):
    return [{"name": c.name, "value": c.value, "domain": c.domain, "port": c.port, "path": c.path,
             "expires": c.expires, "secure": c.secure, "rest": c.__dict__.get("_rest", {}), "version": c.version,
             "comment": c.comment, "comment_url": c.comment_url, "rfc2109": c.rfc2109, "discard": c.discard}
            for c in jar]


def list_to_cookies(rows):
    jar = RequestsCookieJar()
    for row in rows:
        jar.set_cookie(create_cookie(
            name=row["name"], value=row["value"], domain=row.get("domain", ""), port=row.get("port"),
            path=row.get("path", "/"), expires=row.get("expires"), secure=row.get("secure", False),
            rest=row.get("rest", {"HttpOnly": None}), version=row.get("version", 0), comment=row.get("comment"),
            comment_url=row.get("comment_url"), rfc2109=row.get("rfc2109", False), discard=row.get("discard", True)))
    return jar


class Session(requests.Session):
    """Checks for a stop before every request and applies a default timeout."""

    def __init__(self, control=None):
        super().__init__()
        self.control = control

    def request(self, method, url, *args, **kwargs):
        if self.control is not None:
            self.control.check()
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(method, url, *args, **kwargs)


class Http:
    def __init__(self, *, control=None, issues=None):
        self.control = control
        self.issues = issues if issues is not None else set()
        self.proxies = urllib.request.getproxies()  # Follow the system proxy like the browser does.
        self.headers = dict(DEFAULT_HEADERS)
        self.session = self.new_session()
        retry = Retry(total=5, backoff_factor=0.1, raise_on_status=True, status_forcelist=[500, 502, 503, 504])
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._cookies = RequestsCookieJar()
        self.uuid = None
        self.probe = None  # Optional verification experiment, see verification.py.
        self.secrets = set()  # Extra values hidden from verification diagnostics.
        self.video = None  # The video whose progress is being reported, for diagnostics.
        self.batch_started = time.monotonic()

    def new_session(self):
        """A plain session for side requests; it still honours stop requests."""
        return Session(self.control)

    # ---- login state ----------------------------------------------------
    @property
    def cookies(self):
        return self._cookies

    @cookies.setter
    def cookies(self, value):
        if isinstance(value, dict):
            value = cookiejar_from_dict(value)
        elif isinstance(value, list):
            value = list_to_cookies(value)
        self._cookies = value
        if value:
            try:
                self.uuid = json.loads(unquote_plus(value["CASLOGC"]))["uuid"]
            except KeyError as error:
                raise ValueError("Cookies invalid") from error
            self._cookies[f"exitRecod_{self.uuid}"] = "2"

    def require_login(self):
        if not self._cookies:
            error = NotLoggedIn("No cookies found, please login first")
            log.error(error)
            raise error

    def ready(self, cookies=None, headers=None):
        """Switch the session to a course context, or to the login cookies and default headers."""
        self.session.cookies = cookies if cookies else self._cookies.copy()
        self.session.headers = headers if headers else self.headers.copy()
        self.session.proxies = self.proxies.copy()

    def snapshot(self):
        return {"cookies": self.session.cookies.copy(), "headers": self.session.headers.copy()}

    # ---- requests -------------------------------------------------------
    def api(self, url, data, method="POST", content_type="form"):
        replaying = bool(self.probe and self.probe.replaying)
        if method == "POST":
            if content_type == "json":
                self.session.headers.update({"Content-Type": "application/json;charset=UTF-8"})
                if isinstance(data, dict):
                    data = json.dumps(data)
            response = self.session.post(url, data=data, proxies=self.proxies, timeout=10,
                                         allow_redirects=not replaying)
            self.session.headers.pop("Content-Type", None)
        elif method == "GET":
            response = self.session.get(url, params=data, proxies=self.proxies, timeout=10,
                                        allow_redirects=not replaying)
        else:
            raise ValueError(f"Unsupported method: {method}")
        if self.probe and self.probe.replaying:
            self.probe.replay_http_status = response.status_code
            if not 200 <= response.status_code < 300:
                raise RuntimeError("验证实验收到非成功 HTTP 响应，结果记为未知。")
        return response.json()

    def zhidao(self, url, data, *, encrypt=True, ok_code=0, timestamp=True, method="POST", key=VIDEO_KEY,
               content_type="form"):
        """An encrypted request to the shared-course, AI-course or exam gateways."""
        if timestamp:
            stamp = int(time.time()) * 1000  # The pages send whole seconds.
            data["dateFormate"] = stamp
        form = {"secretStr": Cipher(key).encrypt(json.dumps(data))} if encrypt else data
        if timestamp:
            form["dateFormate"] = stamp
        if method == "POST" and content_type == "json":
            form = json.dumps(form)
        probe = self.probe
        captured = probe.capture(url, form, method, content_type) if probe else None
        result = self.api(url, form, method=method, content_type=content_type)
        code = result.get("code")
        if code == -12:
            detail = challenge_details(self, url, result.get("message") or result.get("msg"), (data, form))
            log.warning("平台验证诊断：" + json.dumps(detail, ensure_ascii=False), extra={"diagnostic": True})
            if probe:
                replacement = probe.handle(captured)
                if replacement is not None:
                    return replacement
        if ok_code is not None and code != ok_code:
            if code == -12:
                error = CaptchaRequired("captcha required")
            else:
                error = PlatformError(f"code: {code} msg: {result.get('message') or json.dumps(result, ensure_ascii=False)}")
            if not (probe and probe.internal):
                if code == -12:
                    self.issues.add("verification")
                log.error(error)
            raise error
        if probe and code == 0:
            probe.success(url)
        return result

    def watch_video(self, video_id):
        """Fetch the video address in the background, as the official player does."""
        headers, cookies, proxies = self.session.headers.copy(), self.session.cookies.copy(), self.proxies

        def watch():
            # Two independent sessions, like two separate ``requests.get`` calls.
            try:
                with self.new_session() as session:
                    response = session.get("https://newbase.zhihuishu.com/video/initVideo", params={
                        "jsonpCallBack": "result", "videoID": str(video_id), "_": int(time.time() * 1000)},
                        cookies=cookies, headers=headers, proxies=proxies, timeout=10)
                line = json.loads(re.match(r"^result\((.*)\)$", response.text)[1])["result"]["lines"][0]["lineUrl"]
            except TaskStopped:
                return
            except Exception:
                log.info("视频播放地址获取失败，不影响进度上报：%s", video_id)
                return
            try:
                with self.new_session() as session:
                    session.get(line, headers=headers, cookies=cookies, proxies=proxies)
            except TaskStopped:
                pass
            except Exception:
                log.exception("视频地址读取失败：%s", video_id)

        threading.Thread(target=watch, name="watch-video", daemon=True).start()

    def hike(self, url, data, *, sign=False, ok_code=200, timestamp=True, method="GET"):
        if timestamp:
            data["_"] = int(time.time() * 1000)
        if sign:
            for name, value in data.items():
                data[name] = str(value)
            data["signature"] = hike_signature(data)
        result = self.api(url, data, method=method)
        if ok_code is not None and int(result.get("status")) != ok_code:
            error = PlatformError(f"{result.get('status')} {result.get('message') or json.dumps(result, ensure_ascii=False)}")
            log.error(error)
            raise error
        return result
