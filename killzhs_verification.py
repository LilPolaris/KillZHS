"""Diagnostics and an opt-in, manually acknowledged same-request experiment."""

import copy
import json
import math
import os
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from killzhs_privacy import redact
from killzhs_runtime import TaskStopped


SUPPORTED_PATHS = {"/gateway/t/v1/learning/prelearningNote",
                   "/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"}


def _strings(value):
    if isinstance(value, dict):
        return set().union(*(_strings(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_strings(item) for item in value)) if value else set()
    return {value} if isinstance(value, str) and len(value) >= 4 else set()


def _number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def challenge_details(client, url, message, payload):
    secrets = set(getattr(client, "diagnostic_secrets", ())) | _strings(payload)
    session = getattr(client, "session", None)
    if session is not None:
        secrets.update(cookie.value for cookie in session.cookies if isinstance(cookie.value, str) and len(cookie.value) >= 4)
    secrets.update(_strings(getattr(client, "uuid", None)))
    video = getattr(client, "verification_video", None) or {}
    start = getattr(client, "batch_started_monotonic", time.monotonic())
    return {"path": urlsplit(url).path[:200], "code": -12, "worker_pid": os.getpid(),
            "server_message": re.sub(r"[A-Za-z0-9_+/=-]{24,}", "[已隐藏]",
                                     redact(message if isinstance(message, str) else "", secrets))[:300],
            "video_id": str(video.get("video_id"))[:80] if video.get("video_id") is not None else None,
            "played_time": _number(video.get("played_time")),
            "batch_elapsed_seconds": round(max(0, time.monotonic() - start), 1)}


def _cookies(client):
    return tuple(sorted(((cookie.name, cookie.value, cookie.domain, cookie.path) for cookie in client.session.cookies),
                        key=lambda row: tuple(str(value) for value in row)))


class VerificationProbe:
    """Hold the original envelope in memory; never refresh credentials or context."""

    def __init__(self, client, control, signal_file, task_id, *, enabled=False,
                 report=lambda **_: None, log=lambda *_: None, wait_seconds=1800,
                 clock=time.monotonic, sleep=time.sleep):
        self.client, self.control = client, control
        self.signal_file, self.task_id = signal_file, task_id
        self.enabled, self.report, self.log = enabled, report, log
        self.wait_seconds, self.clock, self.sleep = min(wait_seconds, 1800), clock, sleep
        self.course_kind = None
        self.internal = False
        self.replaying = False
        self.replay_http_status = None
        self.suppress_stop_flush = False
        self.active = None
        self.observation = None

    def capture(self, url, form, method, content_type):
        path = urlsplit(url).path
        cleaning = getattr(self.control.local, "cleaning", False)
        if not self.enabled or self.internal or cleaning or self.course_kind != "zhidao" or path not in SUPPORTED_PATHS:
            return None
        return {"url": url, "form": copy.deepcopy(form), "method": method,
                "captured_at": self.clock(),
                "content_type": content_type, "session": self.client.session,
                "cookies": _cookies(self.client), "headers": dict(self.client.session.headers),
                "proxies": copy.deepcopy(self.client.proxies),
                "video": copy.deepcopy(getattr(self.client, "verification_video", None) or {})}

    def _event(self, phase, **values):
        self.report(phase=phase, probe_id=self.active, task_id=self.task_id, **values)

    def _snapshot(self, video):
        """Read one video's server state, without replacing cached context."""
        self.internal = True
        try:
            ctx = self.client.context.get(video.get("course_id"))
            item = ctx.videos.get(video.get("video_id")) if ctx else None
            if not item:
                return None
            states = self.client.queryStudyInfo([item.lessonId], [item.id] if item.id else [], ctx.course.recruitId)
            row = (states.get("lv") or {}).get(str(item.id)) if item.id else None
            row = row or (states.get("lesson") or {}).get(str(item.lessonId))
            if not row:
                return None
            return {"study_total_time": _number(row.get("studyTotalTime")), "watch_state": row.get("watchState")
                    if isinstance(row.get("watchState"), (int, str)) else None}
        except (Exception, TaskStopped):
            self.log("验证实验：平台进度读取失败，结果记为未知；不会据此重复上报。", "warning")
            return None
        finally:
            self.internal = False

    def _same_context(self, captured):
        return (self.client.session is captured["session"] and _cookies(self.client) == captured["cookies"]
                and dict(self.client.session.headers) == captured["headers"]
                and self.client.proxies == captured["proxies"])

    def _confirmed(self):
        if not self.signal_file:
            return False
        try:
            signal = json.loads(self.signal_file.read_text(encoding="utf-8"))
            return isinstance(signal, dict) and signal.get("task_id") == self.task_id and signal.get("probe_id") == self.active
        except (OSError, ValueError):
            return False

    def handle(self, captured):
        if captured is None or self.internal:
            return None
        self.finish("another_verification")
        self.active = uuid4().hex
        self.suppress_stop_flush = True
        end = self.clock() + self.wait_seconds
        self._event("waiting", timeout_seconds=self.wait_seconds, path=urlsplit(captured["url"]).path,
                    video_id=str(captured["video"].get("video_id")) if captured["video"].get("video_id") is not None else None)
        self.log("验证实验：保留当前 worker、请求参数和 token，等待你完成官网验证；确认前不会重发失败请求。", "warning")
        baseline = self._snapshot(captured["video"])
        self._event("baseline", baseline=baseline)
        try:
            while self.clock() < end:
                self.control.check()
                if self._confirmed():
                    self._event("confirmed", confirmed_at=time.time())
                    if not self._same_context(captured):
                        self._event("ended", outcome="context_changed", normal_reports=0)
                        self.log("验证实验：Cookie、请求头或会话已经变化，不能算作同一请求对照；按原有流程结束。", "warning")
                        return None
                    self.control.check()
                    self._event("replaying", attempt=1, same_payload=True, same_session=True,
                                request_age_seconds=round(max(0, self.clock() - captured["captured_at"]), 1))
                    self.internal = True
                    adapter = self.client.session.get_adapter(captured["url"])
                    retries = adapter.max_retries
                    try:
                        from requests.adapters import Retry
                        adapter.max_retries = Retry(total=0, connect=0, read=0, redirect=0, status=0, other=0)
                        self.replaying = True
                        self.replay_http_status = None
                        response = self.client._apiQuery(captured["url"], data=copy.deepcopy(captured["form"]),
                            method=captured["method"], contentType=captured["content_type"])
                    except Exception:
                        self._event("ended", outcome="transport_error", http_status=self.replay_http_status, normal_reports=0)
                        self.log("验证实验：原请求重发发生网络或解析错误，结果未知；不自动重试。", "warning")
                        return None
                    finally:
                        self.internal = False
                        self.replaying = False
                        adapter.max_retries = retries
                    code = response.get("code") if isinstance(response, dict) else None
                    self._event("replay_result", code=code if isinstance(code, (int, str)) else None,
                                http_status=self.replay_http_status,
                                cookies_unchanged_after_replay=_cookies(self.client) == captured["cookies"])
                    if code != 0:
                        self._event("ended", outcome="original_request_rejected", code=code if isinstance(code, (int, str)) else None,
                                    normal_reports=0)
                        return None
                    self.suppress_stop_flush = False
                    self.observation = {"video": captured["video"], "baseline": baseline, "normal_reports": 0}
                    self._event("observing", normal_reports=0, baseline=baseline)
                    self.log("验证实验：原请求成功，继续观察同一视频的正常上报；尚不能据此认定进度已记入。", "info")
                    return response
                self.sleep(min(.25, max(0, end - self.clock())))
            self._event("ended", outcome="timeout", normal_reports=0)
            self.log("验证实验：等待已满 30 分钟或达到实验等待上限，按原有流程结束。", "warning")
            return None
        except BaseException:
            self._event("ended", outcome="interrupted", normal_reports=0)
            raise

    def success(self, url):
        if self.internal or not self.observation:
            return
        video = getattr(self.client, "verification_video", None) or {}
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
            "progress_delta_seconds": delta, "outcome": reason}, ensure_ascii=False) +
            "；仅是本次记录，不证明网页验证是唯一原因，也不代表整门课程完成。", "info")
