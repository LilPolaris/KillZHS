"""Workbench task state and event handling; platform work runs in a child process."""

import copy
import importlib.util
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from .. import VERSION
from ..models import AIConfig, InputError, NotificationConfig, RunSettings, public_ai, public_notifications
from ..notifications import Notifier
from ..privacy import collect_secrets, redact
from ..runtime import STOP_GRACE_SECONDS
from .actions import course_key, dispatch, normalize_course
from .history import History, TaskLogs, new_task_id
from .store import Preferences, Store, load_courses

DEFAULTS = RunSettings().model_dump()
MODULES = {"requests": "requests", "pycryptodome": "Crypto", "OpenAI": "openai", "tiktoken": "tiktoken", "pydantic": "pydantic"}


class Controller:
    def __init__(self, data_dir=None, demo=False, *, worker_factory=None):
        self.demo = demo
        self.runtime = tempfile.TemporaryDirectory(prefix="killzhs-demo-") if demo else None
        self.local = Path(self.runtime.name if demo else data_dir or ".local/data").resolve()
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.store = Store(self.local)
        self.courses = [{**c.to_dict(), "status": "ready"} for c in load_courses(self.store)]
        if demo:
            from .demo import demo_courses
            self.courses = demo_courses()
        self.preferences = Preferences(self.store, {course_key(c) for c in self.courses})
        for name in ("settings", "ai", "notifications", "plan"):
            setattr(self, name, getattr(self.preferences, name).model_dump())
        self.secrets = collect_secrets(self.ai) | collect_secrets(self.notifications)
        self.cookies = []
        if self.settings["remember"]:
            cookies = self.store.load("cookies.json", [])
            self.cookies = cookies if isinstance(cookies, list) else []
        self.secrets.update(collect_secrets(self.cookies))
        self.task_logs = TaskLogs(self.local, secrets=self.secrets)
        self.history = History(self.store, self.task_logs)
        self.logs = deque(maxlen=600)
        self.sequence = 0
        self.status = "idle"
        self.action = None
        self.logged_in = bool(demo)
        self.qr = self.progress = self.current = None
        self.started_at = self.ended_at = None
        self.result_status = self.result_reason = None
        self.process = None
        self.thread = None
        self.worker_factory = worker_factory
        self.watchdog = None
        self.deadline = None
        self.stop_requested = None
        self.terminating = False
        self.job_id = 0
        self.task_id = None
        self.archive_failed = False
        self.job_courses = []
        self.origin = "manual"
        self.token = secrets.token_urlsafe(32)
        self.ai_requests = 0
        self.active_ai_max_requests = None
        self.assessments = []
        self.assessment_results = {}
        self.verification = self.verification_probe = None
        self.verify_probe_enabled = False
        self.probe_notified = set()
        self.dependencies = [name for name, module in MODULES.items() if importlib.util.find_spec(module) is None]
        self.message = "演示模式 · 所有操作均为本地模拟。" if demo else "准备好后，扫码登录并同步课程。"
        self.notifier = Notifier(lambda message, level: self.log(message, level, archive=False))
        from .scheduler import Scheduler
        self.scheduler = Scheduler(self, self.store)
        self.log("本地工作台已就绪。" + (" 当前为演示模式。" if demo else ""))
        if not demo:
            try:
                recovered = self.history.recover()
                if recovered:
                    self.log(recovered["message"], "warning")
            except OSError:
                self.log("无法恢复上次任务记录，请检查用户目录权限。", "warning")
            self.restore_verification()

    @property
    def history_revision(self):
        return self.history.revision

    @property
    def last_schedule_date(self):
        return self.scheduler.last_date

    @last_schedule_date.setter
    def last_schedule_date(self, value):
        self.scheduler.last_date = value

    def dispatch(self, data, *, scheduled=False, budget=None):
        return dispatch(self, data, scheduled=scheduled, budget=budget)

    def action_request(self, name, params=None):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise InputError("请求必须为 JSON 对象。")
        return self.dispatch({**params, "action": name})

    def save_preference(self, name, model):
        previous = getattr(self.preferences, name)
        setattr(self.preferences, name, model)
        try:
            self.preferences.save()
        except OSError:
            setattr(self.preferences, name, previous)
            raise
        value = model.model_dump()
        setattr(self, name, value)
        self.secrets.update(collect_secrets(value))

    def persist_courses(self):
        self.store.save("courses.json", [{k: v for k, v in c.items() if k != "status"} for c in self.courses])

    def has_session(self):
        return bool(self.cookies) or (self.settings["remember"] and self.store.exists("cookies.json"))

    def state(self, since=None, history_revision=None):
        with self.lock:
            logs_reset = since is None or since > self.sequence or (self.logs and since < self.logs[0]["id"] - 1)
            logs = list(self.logs) if logs_reset else [row for row in self.logs if row["id"] > since]
            result = {"token": self.token, "demo": self.demo, "status": self.status,
                "action": self.action, "logged_in": self.logged_in, "qr": self.qr,
                "progress": self.progress, "current": self.current, "message": self.message,
                "started_at": self.started_at, "ended_at": self.ended_at, "settings": self.settings,
                "courses": self.courses, "logs": logs, "missing_dependencies": self.dependencies,
                "log_cursor": self.sequence, "logs_reset": bool(logs_reset), "defaults": DEFAULTS,
                "saved_session": self.has_session(), "version": VERSION,
                "ai": public_ai(AIConfig.model_validate(self.ai)),
                "plan": self.plan, "next_run": self.scheduler.next_run(),
                "history_revision": self.history_revision, "deadline": self.deadline,
                "ai_requests": self.ai_requests, "active_ai_max_requests": self.active_ai_max_requests,
                "stop_requested": self.stop_requested, "verification": self.verification,
                "verification_probe": self.verification_probe, "verify_probe_enabled": self.verify_probe_enabled,
                "verify_probe_available": os.environ.get("KILLZHS_VERIFY_PROBE") == "1",
                "notifications": public_notifications(NotificationConfig.model_validate(self.notifications)),
                "assessments": self.assessments, "assessment_results": self.assessment_results, "app": "KillZHS"}
            if history_revision != self.history_revision:
                result["history"] = list(reversed(self.history.rows))
            return copy.deepcopy(result)

    def verification_context(self, keys, current=None, task_id=None):
        known = {course_key(course): course for course in self.courses}
        keys = list(dict.fromkeys(key for key in keys if isinstance(key, str) and key in known))
        course = known.get(current) if isinstance(current, str) else None
        if course is None and len(keys) == 1:
            course = known[keys[0]]
        direct = bool(course and course["kind"] == "zhidao")
        url = ("https://studyvideoh5.zhihuishu.com/stuStudy?" + urlencode({"recruitAndCourseId": course["id"]})
               if direct else "https://www.zhihuishu.com/")
        return {"course_key": course_key(course) if course else None,
                "course_name": course["name"] if course else None, "url": url, "direct": direct,
                "course_keys": keys, "task_id": task_id,
                "action": self.action if self.action in ("assessment_scan", "assessment_run") else "start"}

    def restore_verification(self):
        # Older releases saved only a message and names. Restore a direct link
        # only when a name uniquely identifies a course; never reuse a stored URL.
        row = next((row for row in reversed(self.history.rows) if row.get("action") in ("start", "assessment_scan", "assessment_run")), None)
        if not row:
            return
        saved = row.get("verification")
        if row.get("reason") == "verification" and isinstance(saved, dict):
            keys = saved.get("course_keys", [])
            self.verification = self.verification_context(keys if isinstance(keys, list) else [],
                                                        saved.get("course_key"), row.get("id"))
            self.verification["action"] = row["action"]
        elif row.get("status") == "error" and "需要手动验证" in str(row.get("message", "")):
            names = row.get("courses", [])
            if not isinstance(names, list):
                names = []
            keys = []
            for name in names:
                matches = [course_key(course) for course in self.courses if course["name"] == name]
                if len(matches) == 1:
                    keys.append(matches[0])
            self.verification = self.verification_context(keys, task_id=row.get("id"))

    def log(self, message, level="info", *, archive=True):
        with self.lock:
            self.sequence += 1
            row = {"id": self.sequence, "time": datetime.now().astimezone().isoformat(),
                   "message": redact(message, self.secrets), "level": level}
            self.logs.append(row)
            if archive and self.task_id and not self.archive_failed:
                try:
                    self.task_logs.append(self.task_id, row)
                except OSError:
                    self.archive_failed = True
                    self.log("无法保存本次详细日志，请检查用户目录权限或剩余空间。", "warning", archive=False)

    def event(self, event):
        with self.lock:
            event_type = event.get("type")
            if event_type == "log":
                self.log(event.get("message", ""), event.get("level", "info"))
            elif event_type == "qr":
                self.qr = event["image"]
                self.message = "请用知到 App 扫码，并在手机上确认登录。"
            elif event_type == "auth":
                self.logged_in = bool(event["logged_in"])
                self.qr = None
                if not self.logged_in:
                    self.result_reason = "auth"
            elif event_type == "cookies":
                cookies = event.get("cookies")
                if isinstance(cookies, list):
                    self.cookies = copy.deepcopy(cookies)
                    self.secrets.update(collect_secrets(cookies))
                    if self.settings["remember"]:
                        self.store.save("cookies.json", cookies)
            elif event_type == "courses":
                # Never discard a course category when only the other API succeeds.
                kinds = set(event.get("kinds", ["hike", "zhidao", "auto"]))
                rows, invalid_kinds = [], set(event.get("incomplete_kinds", []))
                for row in event["courses"]:
                    try:
                        course = normalize_course(row)
                        if course_key(course) not in {course_key(c) for c in rows}:
                            rows.append(course)
                    except InputError:
                        invalid_kinds.add(row.get("kind") if isinstance(row, dict) else None)
                        self.log("已跳过一条无法识别的课程数据，其余课程继续同步。", "warning")
                # An incomplete category must not erase previously saved entries.
                replace_kinds = kinds - invalid_kinds if None not in invalid_kinds else set()
                self.courses = rows + [c for c in self.courses if c["kind"] not in replace_kinds and course_key(c) not in {course_key(r) for r in rows}]
                self.persist_courses()
            elif event_type == "progress":
                self.progress = {"value": event["value"], "title": event.get("title", "当前视频"),
                                 "phase": event.get("phase", "video"), "remaining": event.get("remaining")}
            elif event_type == "ai_usage":
                self.ai_requests = event["count"]
            elif event_type == "assessments":
                key = event["course_key"]
                known = {course_key(c) for c in self.courses}
                if key not in known:
                    return
                self.assessments = [row for row in self.assessments if row["course_key"] != key] + event["items"]
            elif event_type == "assessment_result":
                self.assessment_results[event["key"]] = {k: v for k, v in event.items() if k != "type"}
            elif event_type == "assessment_course":
                self.current = event["key"]
            elif event_type == "course":
                self.current = event["key"]
                self.progress = None
                for course in self.courses:
                    if course_key(course) == event["key"]:
                        course["status"] = event["status"]
                        if not self.stop_requested:
                            self.message = event.get("message", course["name"])
            elif event_type == "verification_probe":
                if not self.verify_probe_enabled or event.get("task_id") != self.task_id or not re.fullmatch(r"[0-9a-f]{32}", str(event.get("probe_id", ""))):
                    return
                if not self.verification_probe or self.verification_probe.get("probe_id") != event["probe_id"]:
                    self.verification_probe = {"probe_id": event["probe_id"], "task_id": self.task_id}
                self.verification_probe.update({key: value for key, value in event.items()
                                                if key not in ("type", "confirmation_sent", "awaiting_confirmation")})
                phase = event.get("phase")
                if phase == "waiting":
                    self.verification_probe["awaiting_confirmation"] = True
                    self.verification_probe.setdefault("confirmation_sent", False)
                    remaining = {course_key(course) for course in self.courses if course["status"] != "done"}
                    self.verification = self.verification_context([course_key(course) for course in self.job_courses
                        if course_key(course) in remaining], self.current, self.task_id)
                    self.message = "等待验证（实验）：请完成官网验证后，点击“我已完成网页验证”。"
                    if event["probe_id"] not in self.probe_notified:
                        self.probe_notified.add(event["probe_id"])
                        self.notify("verification", "KillZHS 等待验证（实验）", self.message, task_id=self.task_id)
                elif phase in ("confirmed", "replaying", "replay_result", "observing", "observed", "ended"):
                    self.verification_probe["awaiting_confirmation"] = False
                    if phase == "observing":
                        self.verification = None
                        self.message = "验证实验：原请求已成功，正在观察后续正常上报；进度仍需平台确认。"
                self.log("验证实验事件：" + json.dumps({key: value for key, value in event.items()
                    if key not in ("type", "task_id", "probe_id")}, ensure_ascii=False))
            elif event_type == "result":
                self.message = redact(event["message"], self.secrets)
                self.result_status = (event["status"] if event.get("status") in ("stopped", "limited", "error")
                                      else "error" if event.get("error") else "done")
                self.result_reason = event.get("reason") or self.result_reason
                if self.result_reason == "verification":
                    remaining = {course_key(course) for course in self.courses if course["status"] != "done"}
                    keys = [course_key(course) for course in self.job_courses
                            if (self.action or "").startswith("assessment_") or course_key(course) in remaining]
                    self.verification = self.verification_context(keys, self.current, self.task_id)

    def history_row(self):
        return {"id": self.task_id, "started_at": self.started_at, "ended_at": self.ended_at,
            "action": self.action, "origin": self.origin, "status": self.status,
            "message": self.message, "courses": [c["name"] for c in self.job_courses],
            "ai_requests": self.ai_requests, "reason": self.result_reason,
            "verification": copy.deepcopy(self.verification) if self.result_reason == "verification" else None,
            "verification_probe": copy.deepcopy(self.verification_probe)}

    def task_log(self, task_id):
        with self.lock:
            if task_id not in {row.get("id") for row in self.history.rows} | {self.task_id}:
                raise InputError("任务记录不存在，旧版记录可能没有详细日志。")
        try:
            return self.task_logs.read(task_id)
        except (OSError, ValueError) as error:
            raise InputError("详细日志不存在或已清理。") from error

    def notification_report(self, message, level, task_id):
        with self.lock:
            self.log(message, level, archive=False)
            if task_id and task_id in {row.get("id") for row in self.history.rows}:
                try:
                    self.task_logs.append(task_id, {"time": datetime.now().astimezone().isoformat(),
                                                  "level": level, "message": redact(message, self.secrets)})
                except OSError:
                    self.log("无法保存通知结果到任务日志。", "warning", archive=False)

    def notify(self, event, title, body, *, task_id=None):
        if not self.demo:
            self.notifier.submit(self.notifications, event, title, body,
                                 report=lambda message, level: self.notification_report(message, level, task_id))

    def begin_job(self, action, courses, assessments, *, scheduled=False, budget=None):
        """Called under ``lock`` after the action has validated its inputs."""
        self.status, self.action = "running", action
        self.job_id += 1
        job_id = self.job_id
        self.job_courses = copy.deepcopy(courses)
        self.origin = "schedule" if scheduled else "manual"
        self.ai_requests = 0
        self.active_ai_max_requests = self.ai["max_requests"] if action in ("start", "assessment_run") else None
        if action == "assessment_run":
            self.assessment_results = {}
        self.result_status = self.result_reason = self.ended_at = None
        self.verify_probe_enabled = action == "start" and os.environ.get("KILLZHS_VERIFY_PROBE") == "1"
        self.verification_probe = None
        self.probe_notified.clear()
        if action == "start":
            self.verification = None
        self.stop_requested = None
        self.terminating = False
        self.qr = self.progress = self.current = None
        self.started_at = time.time()
        self.task_id, self.archive_failed = new_task_id(), False
        self.message = {"login": "正在连接登录服务…", "refresh": "正在同步课程…", "start": "正在准备学习任务…",
                        "test_ai": "正在检查模型服务…", "diagnose": "正在检查运行环境…",
                        "assessment_scan": "正在同步章节测验与考试…", "assessment_run": "正在自动答题并交卷…"}[action]
        minutes = self.settings["budget_minutes"] if budget is None else budget
        self.deadline = self.started_at + minutes * 60 if action == "start" and minutes else None
        selected = {course_key(course) for course in courses}
        if action == "start":
            for course in self.courses:
                course["status"] = "queued" if course_key(course) in selected else "ready"
        try:
            self.history.mark_active(self.history_row())
        except OSError:
            self.archive_failed = True
            self.log("无法保存任务日志索引，本次仅保留会话日志。", "warning", archive=False)
        self.log("任务开始：" + action)
        payload = {"action": action, "settings": copy.deepcopy(self.settings), "ai": copy.deepcopy(self.ai),
                   "courses": copy.deepcopy(courses), "assessments": copy.deepcopy(assessments),
                   "verify_probe_enabled": self.verify_probe_enabled, "task_id": self.task_id,
                   "started_at": self.started_at, "deadline": self.deadline, "cookies": copy.deepcopy(self.cookies)}
        try:
            factory = self.worker_factory
            if factory is None:
                if self.demo:
                    from .demo import DemoProcess
                    factory = DemoProcess
                else:
                    from ..worker.process import WorkerProcess
                    factory = WorkerProcess
            self.process = factory(payload, self.local,
                                   lambda event: self._worker_event(job_id, event),
                                   lambda code: self._worker_exit(job_id, code))
            self.process.start()
            self.thread = getattr(self.process, "thread", None)
            if self.deadline and self.status == "running":
                self._arm_timer(max(0, self.deadline - time.time()), self.expire, job_id)
        except Exception:
            self.result_status = "error"
            self.message = "无法启动运行进程，请检查 Python 环境。"
            self._worker_exit(job_id, 1)

    def _arm_timer(self, delay, callback, *args):
        if self.watchdog:
            self.watchdog.cancel()
        self.watchdog = threading.Timer(delay, callback, args=args)
        self.watchdog.daemon = True
        self.watchdog.start()

    def _worker_event(self, job_id, event):
        with self.lock:
            if self.job_id != job_id or self.status != "running" or self.terminating:
                return
            try:
                self.event(event)
            except OSError:
                self.log("无法保存任务数据，请检查用户目录权限或剩余空间。", "warning", archive=False)

    def _worker_exit(self, job_id, exitcode):
        with self.lock:
            if self.job_id != job_id or self.status != "running":
                return
            if self.watchdog:
                self.watchdog.cancel()
                self.watchdog = None
            self.status = self.stop_requested or self.result_status or "error"
            if exitcode and self.status not in ("stopped", "limited", "error"):
                self.status = "error"
                self.message = "运行进程意外退出，请查看本次日志。"
            if not self.result_status and not self.stop_requested:
                self.message = ("运行进程意外退出，请查看本次日志。" if exitcode
                                else "运行进程未返回任务结果，请查看本次日志。")
            self.stop_requested = None
            self.ended_at = time.time()
            self.process, self.qr = None, None
            if self.verification_probe:
                self.verification_probe["awaiting_confirmation"] = False
            if self.action == "assessment_run":
                for result in self.assessment_results.values():
                    if result.get("state") == "running":
                        result.update(state=self.status if self.status in ("stopped", "limited") else "attention",
                                      message=self.message)
            for course in self.courses:
                if course["status"] in ("queued", "running"):
                    course["status"] = self.status if self.status in ("stopped", "limited") else "attention"
            self.record_history()

    def record_history(self):
        self.log(self.message, "warning" if self.status in ("error", "limited", "stopped") else "info")
        row = self.history_row()
        row["has_log"] = self.task_logs.exists(self.task_id)
        self.task_id = None
        try:
            self.history.append(row)
            self.history.clear_active()
        except OSError:
            self.log("无法保存任务历史，请检查用户目录权限。", "warning", archive=False)
        if self.action in ("start", "assessment_run") or self.result_reason == "auth":
            event = self.result_reason if self.result_reason in ("verification", "auth") else "finished"
            title = {"verification": "KillZHS 需要手动验证", "auth": "KillZHS 登录状态需要检查"}.get(
                event, "KillZHS 任务需要查看" if self.status == "error" else "KillZHS 本次任务已结束")
            body = {"verification": "本批次已停止。请打开知到官方页面完成验证后，再手动开始任务。",
                    "auth": "当前任务无法使用或确认登录状态，请先检查网络并同步课程，必要时重新扫码。"}.get(
                        event, "请在历史记录中查看本次结果和详细日志。课程完成情况以平台为准。")
            if event != "verification" or not self.probe_notified:
                self.notify(event, title, body, task_id=row["id"])

    def expire(self, job_id):
        self.stop(reason="limited", expected_job=job_id)

    def force_stop_after(self, job_id, reason):
        self.stop(reason=reason, expected_job=job_id, force=True)

    def stop(self, reason="stopped", expected_job=None, *, force=False):
        with self.lock:
            if self.status != "running" or (expected_job is not None and expected_job != self.job_id):
                return
            job_id, process = self.job_id, self.process
            if self.action == "start" and not force:
                if self.stop_requested:
                    return
                try:
                    if process is None:
                        raise OSError("worker unavailable")
                    process.request_stop(reason)
                except (OSError, ValueError):
                    self.log("无法发送停止请求，将直接终止运行进程。", "warning")
                    force = True
                else:
                    self.stop_requested = reason
                    self.message = ("已到总时长" if reason == "limited" else "已请求停止") + f"，正在收尾并尝试保存最后进度，最多等待 {STOP_GRACE_SECONDS} 秒。"
                    self.log(self.message, "warning")
                    self._arm_timer(STOP_GRACE_SECONDS, self.force_stop_after, job_id, reason)
                    return
            if self.watchdog:
                self.watchdog.cancel()
                self.watchdog = None
            requested = bool(self.stop_requested)
            self.stop_requested = reason
            self.message = ("已达到本次总时长，任务已结束。进度以平台最近一次接收为准。" if reason == "limited"
                            else "任务已停止。已提交的平台进度会保留。")
            if requested:
                self.message += " 收尾超时或程序退出，已请求强制结束。"
            # Stop accepting late course/result events while terminating the process.
            self.result_status = reason
            self.terminating = True
        # Joining a worker while holding lock deadlocks its event reader.
        if process:
            try:
                process.terminate()
                process.join(timeout=3)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
                if process.is_alive():
                    self.log("运行进程未能及时退出，请检查后台进程。", "warning")
                    return
            except (OSError, ValueError):
                self.log("运行进程未能及时退出，请检查后台进程。", "warning")
                return
        self._worker_exit(job_id, 0)

    def start_scheduler(self):
        self.scheduler.start()

    def schedule_tick(self, now=None):
        return self.scheduler.tick(now)

    def close(self):
        self.closed.set()
        self.scheduler.close()
        self.stop(force=True)
        self.notifier.close()
        if self.runtime:
            self.runtime.cleanup()
