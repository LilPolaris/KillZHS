"""Verification diagnostics and the opt-in, manually confirmed same-request experiment.

When a shared-course progress request is answered with ``code: -12`` and the
experiment is enabled, the worker keeps the original request envelope in memory,
waits for the person to confirm they finished the official web verification, and
re-sends exactly that request once. It never refreshes credentials or context.
"""

import copy
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from ..privacy import redact
from ..runtime import TaskStopped

log = logging.getLogger("killzhs.platform")

SUPPORTED_PATHS = {"/gateway/t/v1/learning/prelearningNote", "/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"}
WAIT_LIMIT_SECONDS = 1800


def _strings(value):
    if isinstance(value, dict):
        return set().union(*(_strings(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_strings(item) for item in value)) if value else set()
    return {value} if isinstance(value, str) and len(value) >= 4 else set()


def _number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def challenge_details(http, url, message, payload):
    """A redacted description of a ``code: -12`` answer, safe for the task log."""
    secrets = set(http.secrets) | _strings(payload) | _strings(http.uuid)
    secrets.update(cookie.value for cookie in http.session.cookies if isinstance(cookie.value, str) and len(cookie.value) >= 4)
    video = getattr(http, "video", None) or {}
    start = getattr(http, "batch_started", time.monotonic())
    return {"path": urlsplit(url).path[:200], "code": -12, "worker_pid": os.getpid(),
            "server_message": re.sub(r"[A-Za-z0-9_+/=-]{24,}", "[已隐藏]",
                                     redact(message if isinstance(message, str) else "", secrets))[:300],
            "video_id": str(video.get("video_id"))[:80] if video.get("video_id") is not None else None,
            "played_time": _number(video.get("played_time")),
            "batch_elapsed_seconds": round(max(0, time.monotonic() - start), 1)}


def _cookies(http):
    return tuple(sorted(((c.name, c.value, c.domain, c.path) for c in http.session.cookies),
                        key=lambda row: tuple(str(value) for value in row)))


class VerificationProbe:
    def __init__(self, http, shared, control, confirmed, task_id, *, enabled=False, report=lambda **_: None,
                 log=lambda *_: None, wait_seconds=WAIT_LIMIT_SECONDS, clock=time.monotonic, sleep=time.sleep):
        """``confirmed`` returns the probe id the person confirmed in the page, or ``None``."""
        self.http, self.shared, self.control = http, shared, control
        self.confirmed, self.task_id = confirmed, task_id
        self.enabled, self.report, self.log = enabled, report, log
        self.wait_seconds, self.clock, self.sleep = min(wait_seconds, WAIT_LIMIT_SECONDS), clock, sleep
        self.course_kind = None
        self.internal = False
        self.replaying = False
        self.replay_http_status = None
        self.suppress_stop_flush = False
        self.active = None
        self.observation = None

    def capture(self, url, form, method, content_type):
        path = urlsplit(url).path
        if not self.enabled or self.internal or self.control.cleaning or self.course_kind != "zhidao" \
                or path not in SUPPORTED_PATHS:
            return None
        return {"url": url, "form": copy.deepcopy(form), "method": method, "captured_at": self.clock(),
                "content_type": content_type, "session": self.http.session, "cookies": _cookies(self.http),
                "headers": dict(self.http.session.headers), "proxies": copy.deepcopy(self.http.proxies),
                "video": copy.deepcopy(self.http.video or {})}

    def _event(self, phase, **values):
        self.report(phase=phase, probe_id=self.active, task_id=self.task_id, **values)

    def _snapshot(self, video):
        """Read one video's server state without replacing the cached course context."""
        self.internal = True
        try:
            context = self.shared.contexts.get(video.get("course_id"))
            item = context["videos"].get(video.get("video_id")) if context else None
            if not item:
                return None
            states = self.shared.study_info([item["lessonId"]], [item["id"]] if item["id"] else [],
                                            context["course"]["recruitId"])
            row = (states.get("lv") or {}).get(str(item["id"])) if item["id"] else None
            row = row or (states.get("lesson") or {}).get(str(item["lessonId"]))
            if not row:
                return None
            watch = row.get("watchState")
            return {"study_total_time": _number(row.get("studyTotalTime")),
                    "watch_state": watch if isinstance(watch, (int, str)) else None}
        except (Exception, TaskStopped):
            self.log("验证实验：平台进度读取失败，结果记为未知；不会据此重复上报。", "warning")
            return None
        finally:
            self.internal = False

    def _same_context(self, captured):
        return (self.http.session is captured["session"] and _cookies(self.http) == captured["cookies"]
                and dict(self.http.session.headers) == captured["headers"] and self.http.proxies == captured["proxies"])

    def handle(self, captured):
        if captured is None or self.internal:
            return None
        self.finish("another_verification")
        self.active = uuid4().hex
        self.suppress_stop_flush = True
        end = self.clock() + self.wait_seconds
        video_id = captured["video"].get("video_id")
        self._event("waiting", timeout_seconds=self.wait_seconds, path=urlsplit(captured["url"]).path,
                    video_id=str(video_id) if video_id is not None else None)
        self.log("验证实验：保留当前 worker、请求参数和 token，等待你完成官网验证；确认前不会重发失败请求。", "warning")
        baseline = self._snapshot(captured["video"])
        self._event("baseline", baseline=baseline)
        try:
            while self.clock() < end:
                self.control.check()
                if self.confirmed() == self.active:
                    return self._replay(captured, baseline)
                self.sleep(min(.25, max(0, end - self.clock())))
            self._event("ended", outcome="timeout", normal_reports=0)
            self.log("验证实验：等待已满 30 分钟或达到实验等待上限，按原有流程结束。", "warning")
            return None
        except BaseException:
            self._event("ended", outcome="interrupted", normal_reports=0)
            raise

    def _replay(self, captured, baseline):
        self._event("confirmed", confirmed_at=time.time())
        if not self._same_context(captured):
            self._event("ended", outcome="context_changed", normal_reports=0)
            self.log("验证实验：Cookie、请求头或会话已经变化，不能算作同一请求对照；按原有流程结束。", "warning")
            return None
        self.control.check()
        self._event("replaying", attempt=1, same_payload=True, same_session=True,
                    request_age_seconds=round(max(0, self.clock() - captured["captured_at"]), 1))
        self.internal = True
        adapter = self.http.session.get_adapter(captured["url"])
        retries = adapter.max_retries
        try:
            from requests.adapters import Retry
            adapter.max_retries = Retry(total=0, connect=0, read=0, redirect=0, status=0, other=0)
            self.replaying = True
            self.replay_http_status = None
            response = self.http.api(captured["url"], copy.deepcopy(captured["form"]), method=captured["method"],
                                     content_type=captured["content_type"])
        except Exception:
            self._event("ended", outcome="transport_error", http_status=self.replay_http_status, normal_reports=0)
            self.log("验证实验：原请求重发发生网络或解析错误，结果未知；不自动重试。", "warning")
            return None
        finally:
            self.internal = False
            self.replaying = False
            adapter.max_retries = retries
        code = response.get("code") if isinstance(response, dict) else None
        code = code if isinstance(code, (int, str)) else None
        self._event("replay_result", code=code, http_status=self.replay_http_status,
                    cookies_unchanged_after_replay=_cookies(self.http) == captured["cookies"])
        if code != 0:
            self._event("ended", outcome="original_request_rejected", code=code, normal_reports=0)
            return None
        self.suppress_stop_flush = False
        self.observation = {"video": captured["video"], "baseline": baseline, "normal_reports": 0}
        self._event("observing", normal_reports=0, baseline=baseline)
        self.log("验证实验：原请求成功，继续观察同一视频的正常上报；尚不能据此认定进度已记入。", "info")
        return response

    def success(self, url):
        if self.internal or not self.observation:
            return
        video = self.http.video or {}
        expected = self.observation["video"]
        if (video.get("course_id"), video.get("video_id")) != (expected.get("course_id"), expected.get("video_id")):
            self.finish("video_changed")
            return
        if urlsplit(url).path != "/gateway/t/v1/learning/saveDatabaseIntervalTimeV2":
            return
        self.observation["normal_reports"] += 1
        count = self.observation["normal_reports"]
        self._event("observing", normal_reports=count)
        if count >= 3:
            self.finish("three_normal_reports")

    def finish(self, reason):
        observation, self.observation = self.observation, None
        if observation is None:
            return
        after = self._snapshot(observation["video"])
        before = observation["baseline"]
        delta = None
        if before and after and before["study_total_time"] is not None and after["study_total_time"] is not None:
            delta = after["study_total_time"] - before["study_total_time"]
        self._event("observed", outcome=reason, normal_reports=observation["normal_reports"],
                    baseline=before, after=after, progress_delta_seconds=delta)
        self.log("验证实验观察：" + json.dumps({"normal_reports": observation["normal_reports"],
                 "progress_delta_seconds": delta, "outcome": reason}, ensure_ascii=False)
                 + "；仅是本次记录，不证明网页验证是唯一原因，也不代表整门课程完成。", "info")
