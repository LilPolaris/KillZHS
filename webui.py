"""Local browser UI. Only the worker imports the original course client."""

import argparse
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import urlopen
import webbrowser
from killzhs_config import (AI_DEFAULTS, PLAN_DEFAULTS, VERSION, InputError,
                           public_ai, validate_ai, validate_plan, next_run)

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "web"
PREFIX = "@@ZHS_WEB@@"
DEFAULTS = {"speed": None, "limit": 0, "threshold": 0.91, "remember": True, "budget_minutes": 0}
MODULES = {"requests": "requests", "Pillow": "PIL", "pycryptodome": "Crypto",
           "OpenAI": "openai", "tiktoken": "tiktoken"}


def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return copy.deepcopy(fallback)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise InputError("设置格式不正确。")
    result = {**DEFAULTS, **value}
    for key, low, high in (("speed", 0.1, 16), ("limit", 0, 1440), ("threshold", 0.1, 2), ("budget_minutes", 0, 240)):
        number = result[key]
        if key == "speed" and number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise InputError("请输入有效的数字。")
        if not low <= number <= high or (key in ("limit", "budget_minutes") and int(number) != number):
            raise InputError("参数超出范围，请检查倍速、时限或完成阈值。")
    if not isinstance(result["remember"], bool):
        raise InputError("记住登录状态必须为开关值。")
    return result


def normalize_course(row):
    if not isinstance(row, dict):
        raise InputError("课程格式不正确。")
    identifier = str(row.get("id", "")).strip()
    if not identifier or len(identifier) > 256 or not all(c.isascii() and (c.isalnum() or c in "_-=") for c in identifier):
        raise InputError("课程 ID 不正确，请粘贴课程地址中的 courseId 或 recruitAndCourseId。")
    kind = row.get("kind", "auto")
    if kind not in ("auto", "hike", "zhidao", "ai"):
        raise InputError("课程类型不正确。")
    result = {"id": identifier, "name": str(row.get("name") or f"课程 {identifier}")[:160],
              "kind": kind, "status": "ready"}
    if kind == "ai":
        class_id = str(row.get("class_id", "")).strip()
        if not identifier.isdecimal() or not class_id.isascii() or not class_id.isdecimal() or len(class_id) > 30:
            raise InputError("AI 课程需要数字课程 ID 和班级 ID。")
        result["class_id"] = class_id
    return result


def course_key(course):
    return course["kind"] + ":" + course["id"] + (":" + course["class_id"] if course["kind"] == "ai" else "")


class Controller:
    def __init__(self, root=ROOT, demo=False, worker_command=None, data_dir=None):
        self.root = Path(root)
        self.local = Path(data_dir).resolve() if data_dir else self.root / ".local" / "webui"
        self.demo = demo
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.worker_command = worker_command
        self.closed = threading.Event()
        self.scheduler_thread = None
        self.job_id = 0
        self.watchdog = None
        self.deadline = None
        self.job_courses = []
        self.origin = "manual"
        self.token = secrets.token_urlsafe(32)
        self.runtime = tempfile.TemporaryDirectory(prefix="zhs-web-")
        stored = load_json(self.local / "settings.json", {})
        try:
            self.settings = validate_settings({k: v for k, v in stored.items() if k in DEFAULTS}) if isinstance(stored, dict) else DEFAULTS.copy()
        except InputError:
            self.settings = DEFAULTS.copy()
        try:
            self.ai = validate_ai(load_json(self.local / "ai.json", {}))
        except InputError:
            self.ai = copy.deepcopy(AI_DEFAULTS)
        self.history = load_json(self.local / "history.json", [])
        if not isinstance(self.history, list):
            self.history = []
        self.history = self.history[-100:]
        schedule_state = load_json(self.local / "schedule-state.json", {})
        self.last_schedule_date = schedule_state.get("date") if isinstance(schedule_state, dict) else None
        self.courses = []
        self.logs = deque(maxlen=600)
        self.sequence = 0
        self.status = "idle"
        self.action = None
        self.logged_in = False
        self.qr = None
        self.progress = None
        self.started_at = None
        self.ended_at = None
        self.result_status = None
        self.current = None
        self.message = "准备好后，扫码登录并同步课程。"
        self.dependencies = [name for name, module in MODULES.items() if importlib.util.find_spec(module) is None]
        raw_courses = load_json(self.local / "courses.json", load_json(self.root / "execution.json", []))
        if isinstance(raw_courses, list):
            for row in raw_courses:
                try:
                    course = normalize_course(row)
                    if course_key(course) not in {course_key(c) for c in self.courses}:
                        self.courses.append(course)
                except InputError:
                    continue
        if demo:
            self.courses = [normalize_course({"id": ident, "name": name, "kind": kind}) for ident, name, kind in (
                ("demo01", "艺术与审美", "zhidao"), ("demo02", "大学生心理健康", "zhidao"),
                ("10003", "人工智能与信息社会", "hike"), ("10004", "中国传统文化", "hike"))]
            self.logged_in = True
            self.message = "演示模式 · 所有操作均为本地模拟。"
        try:
            self.plan = validate_plan(load_json(self.local / "plan.json", {}), {course_key(c) for c in self.courses})
        except InputError:
            self.plan = copy.deepcopy(PLAN_DEFAULTS)
        self.ai_requests = 0
        self.active_ai_max_requests = None
        self.log("本地工作台已就绪。" + (" 当前为演示模式。" if demo else ""))

    def log(self, message, level="info"):
        with self.lock:
            self.sequence += 1
            self.logs.append({"id": self.sequence, "time": datetime.now().astimezone().isoformat(),
                              "message": str(message)[:1500], "level": level})

    def state(self):
        with self.lock:
            return copy.deepcopy({"token": self.token, "demo": self.demo, "status": self.status,
                "action": self.action, "logged_in": self.logged_in, "qr": self.qr,
                "progress": self.progress, "current": self.current, "message": self.message,
                "started_at": self.started_at, "ended_at": self.ended_at, "settings": self.settings,
                "courses": self.courses, "logs": list(self.logs), "missing_dependencies": self.dependencies,
                "saved_session": self.has_session(), "version": VERSION, "ai": public_ai(self.ai),
                "plan": self.plan, "next_run": next_run(self.plan, self.last_schedule_date),
                "history": list(reversed(self.history)), "deadline": self.deadline,
                "ai_requests": self.ai_requests, "active_ai_max_requests": self.active_ai_max_requests,
                "app": "KillZHS"})

    def has_session(self):
        return (Path(self.runtime.name) / "cookies.json").is_file() or (self.settings["remember"] and (
            (self.local / "cookies.json").is_file() or (self.root / "cookies.json").is_file()))

    def dispatch(self, data, *, scheduled=False, budget=None):
        if not isinstance(data, dict):
            raise InputError("请求必须为 JSON 对象。")
        action = data.get("action")
        if action == "stop":
            self.stop()
            return
        with self.lock:
            if action == "ai_settings":
                updated = validate_ai(data.get("ai"), self.ai)
                if not self.demo:
                    atomic_json(self.local / "ai.json", updated)
                self.ai = updated
                self.log("AI 设置已保存，将在下次任务生效，没有发送模型请求。")
                return
            if self.status == "running" or (self.thread and self.thread.is_alive()):
                raise InputError("已有任务正在运行，请等待结束或先停止。")
            if action == "settings":
                settings = validate_settings(data.get("settings"))
                if not settings["remember"] and self.plan["enabled"]:
                    raise InputError("请先暂停定时计划，再关闭记住登录状态。")
                self.settings = settings
                if not self.demo:
                    atomic_json(self.local / "settings.json", self.settings)
                    if not self.settings["remember"]:
                        (self.local / "cookies.json").unlink(missing_ok=True)
                self.log("运行设置已保存。")
                return
            if action == "plan":
                plan = validate_plan(data.get("plan"), {course_key(c) for c in self.courses})
                if plan["enabled"] and not self.settings["remember"]:
                    raise InputError("定时任务需要记住登录状态，请先在偏好设置中开启。")
                self.plan = plan
                if not self.demo:
                    atomic_json(self.local / "plan.json", plan)
                self.log("定时计划已保存。" if plan["enabled"] else "定时计划已暂停。")
                return
            if action == "pause_plan":
                self.plan["enabled"] = False
                if not self.demo:
                    atomic_json(self.local / "plan.json", self.plan)
                return
            if action == "add":
                course = normalize_course(data.get("course"))
                if course_key(course) in {course_key(c) for c in self.courses}:
                    raise InputError("这门课程已经在清单中了。")
                self.courses.append(course)
                self.persist_courses()
                return
            if action not in ("login", "refresh", "start", "test_ai", "diagnose"):
                raise InputError("不支持的操作。")
            if self.dependencies and not self.demo:
                raise InputError("当前 Python 环境缺少依赖：" + "、".join(self.dependencies) + "。请用项目虚拟环境启动。")
            selected = data.get("courses", [])
            if action == "start":
                if not self.logged_in and not self.has_session() and not self.demo:
                    raise InputError("请先登录并同步课程。")
                if not isinstance(selected, list) or not selected or len(selected) > 200:
                    raise InputError("请至少选择一门课程。")
                known = {course_key(c): c for c in self.courses}
                if any(not isinstance(key, str) or key not in known for key in selected):
                    raise InputError("课程清单已变化，请重新选择。")
                selected = [copy.deepcopy(known[key]) for key in dict.fromkeys(selected)]
                if any(c["kind"] == "ai" for c in selected) and not self.ai["no_exam"]:
                    if not self.ai["enabled"]:
                        raise InputError("AI 答题尚未启用。请开启 AI，或选择跳过测验。")
                    if not self.ai["use_zhidao_ai"] and not (self.ai["openai"]["api_key"] and self.ai["openai"]["model_name"]):
                        raise InputError("请填写自定义 AI 的密钥和模型名称，或选择跳过测验。")
            else:
                selected = []
            self.status, self.action = "running", action
            self.job_id += 1
            self.job_courses, self.origin = copy.deepcopy(selected), "schedule" if scheduled else "manual"
            self.ai_requests = 0
            self.active_ai_max_requests = self.ai["max_requests"] if action == "start" else None
            self.result_status, self.ended_at = None, None
            self.qr, self.progress, self.current = None, None, None
            self.started_at = time.time()
            self.message = {"login": "正在连接登录服务…", "refresh": "正在同步课程…", "start": "正在准备学习任务…", "test_ai": "正在检查模型服务…", "diagnose": "正在检查运行环境…"}[action]
            minutes = self.settings["budget_minutes"] if budget is None else budget
            self.deadline = self.started_at + minutes * 60 if action == "start" and minutes else None
            for course in self.courses:
                if action == "start":
                    course["status"] = "ready"
                if course_key(course) in {course_key(c) for c in selected}:
                    course["status"] = "queued"
            payload = {"action": action, "root": str(self.root), "local": str(self.local),
                       "runtime": self.runtime.name, "settings": copy.deepcopy(self.settings),
                       "ai": copy.deepcopy(self.ai), "courses": selected}
            if self.deadline:
                job_id = self.job_id
                self.watchdog = threading.Timer(max(0, self.deadline - time.time()), self.expire, args=(job_id,))
                self.watchdog.daemon = True
                self.watchdog.start()
            self.thread = threading.Thread(target=self.run_job, args=(payload,), daemon=True)
            self.thread.start()

    def persist_courses(self):
        if not self.demo:
            atomic_json(self.local / "courses.json", [{k: v for k, v in c.items() if k != "status"} for c in self.courses])

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
            elif event_type == "courses":
                # Never discard a course category when only the other API succeeds.
                kinds = set(event.get("kinds", ["hike", "zhidao", "auto"]))
                rows = [normalize_course(row) for row in event["courses"]]
                self.courses = rows + [c for c in self.courses if c["kind"] not in kinds and course_key(c) not in {course_key(r) for r in rows}]
                self.persist_courses()
            elif event_type == "progress":
                self.progress = {"value": event["value"], "title": event.get("title", "当前视频")}
            elif event_type == "ai_usage":
                self.ai_requests = event["count"]
            elif event_type == "course":
                self.current = event["key"]
                self.progress = None
                for course in self.courses:
                    if course_key(course) == event["key"]:
                        course["status"] = event["status"]
                        self.message = event.get("message", course["name"])
            elif event_type == "result":
                self.message = event["message"]
                self.result_status = "error" if event.get("error") else "done"

    def run_job(self, payload):
        process = None
        try:
            if self.demo:
                self.run_demo(payload)
                return
            frozen = getattr(sys, "frozen", False) and not self.worker_command
            job_folder = Path(self.runtime.name) / str(self.job_id)
            if frozen:
                job_folder.mkdir(parents=True, exist_ok=True)
                atomic_json(job_folder / "input.json", payload)
                (job_folder / "events.jsonl").touch()
                command = [sys.executable, "--worker", str(job_folder / "input.json"), str(job_folder / "events.jsonl")]
            else:
                command = self.worker_command or [sys.executable, "-u", str(ROOT / "webui_worker.py")]
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            with self.lock:
                if self.status != "running":
                    return
                self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL if frozen else subprocess.PIPE,
                    stdout=subprocess.DEVNULL if frozen else subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    cwd=self.runtime.name, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                process = self.process
                if not frozen:
                    process.stdin.write(json.dumps(payload) + "\n")
                    process.stdin.close()
            stream = self.file_events(process, job_folder / "events.jsonl") if frozen else process.stdout
            for line in stream:
                if line.startswith(PREFIX):
                    with self.lock:
                        if self.status in ("stopped", "limited"):
                            continue
                        try:
                            self.event(json.loads(line[len(PREFIX):]))
                        except (ValueError, KeyError, TypeError):
                            self.log("收到一条无法解析的运行消息。", "warning")
                # Raw tracebacks may contain cookies; only structured events reach the UI.
            process.wait()
            with self.lock:
                if self.status == "running":
                    if self.result_status is None:
                        self.message = "任务异常结束，请检查 Python 依赖或查看日志。" if process.returncode else "任务已结束。"
                    self.result_status = "error" if process.returncode else self.result_status or "done"
        except Exception:
            with self.lock:
                if self.status not in ("stopped", "limited"):
                    self.status, self.message = "error", "无法启动运行进程，请检查 Python 环境。"
                    self.log(self.message, "error")
        finally:
            if self.watchdog:
                self.watchdog.cancel()
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            with self.lock:
                if self.status == "running":
                    self.status = self.result_status or "done"
                self.ended_at = time.time()
                self.process, self.qr = None, None
                for course in self.courses:
                    if course["status"] in ("queued", "running"):
                        course["status"] = self.status if self.status in ("stopped", "limited") else "attention"
                self.record_history()

    @staticmethod
    def file_events(process, path):
        with path.open(encoding="utf-8") as stream:
            buffered = ""
            while True:
                line = stream.readline()
                if line:
                    buffered += line
                    if buffered.endswith("\n"):
                        yield buffered
                        buffered = ""
                elif process.poll() is not None:
                    return
                else:
                    time.sleep(0.05)

    def record_history(self):
        self.history.append({"started_at": self.started_at, "ended_at": self.ended_at,
            "action": self.action, "origin": self.origin, "status": self.status,
            "message": self.message, "courses": [c["name"] for c in self.job_courses],
            "ai_requests": self.ai_requests})
        self.history = self.history[-100:]
        if not self.demo:
            atomic_json(self.local / "history.json", self.history)

    def expire(self, job_id):
        self.stop(reason="limited", expected_job=job_id)

    def start_scheduler(self):
        def loop():
            while not self.closed.wait(1):
                try:
                    self.schedule_tick()
                except OSError:
                    self.log("无法保存计划运行状态，本次未执行。请检查用户目录权限。", "error")
        self.scheduler_thread = threading.Thread(target=loop, daemon=True)
        self.scheduler_thread.start()

    def schedule_tick(self, now=None):
        now = now or datetime.now().astimezone()
        today = now.date().isoformat()
        with self.lock:
            if self.closed.is_set() or not self.plan["enabled"] or now.weekday() not in self.plan["weekdays"] or self.last_schedule_date == today:
                return
            hour, minute = map(int, self.plan["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < due:
                return
            # Save the attempt before starting, including missed or blocked attempts.
            if not self.demo:
                atomic_json(self.local / "schedule-state.json", {"date": today})
            self.last_schedule_date = today
            reason = None
            if (now - due).total_seconds() >= 60:
                reason = "已错过计划时间，当天不补跑。"
            elif self.status == "running":
                reason = "已有任务正在运行，本次定时任务已跳过。"
            else:
                try:
                    self.dispatch({"action": "start", "courses": self.plan["courses"]}, scheduled=True, budget=self.plan["minutes"])
                    return
                except InputError as error:
                    reason = str(error)
            self.log("定时任务未启动：" + reason, "warning")
            self.history.append({"started_at": now.timestamp(), "ended_at": now.timestamp(), "action": "start",
                "origin": "schedule", "status": "skipped", "message": reason, "courses": [], "ai_requests": 0})
            self.history = self.history[-100:]
            if not self.demo:
                atomic_json(self.local / "history.json", self.history)

    def run_demo(self, payload):
        self.log("演示操作：" + payload["action"] + "，不会连接智慧树。")
        if payload["action"] == "start":
            for course in payload["courses"]:
                with self.lock:
                    if self.status != "running":
                        return
                    self.event({"type": "course", "key": course_key(course), "status": "running"})
                for value in range(0, 101, 5):
                    time.sleep(0.18)
                    with self.lock:
                        if self.status != "running":
                            return
                        self.event({"type": "progress", "value": value, "title": "示例视频 · 本地模拟"})
                self.event({"type": "course", "key": course_key(course), "status": "done"})
                self.log(course["name"] + " · 演示任务已运行。")
        else:
            time.sleep(0.5)
            self.event({"type": "auth", "logged_in": True})
        with self.lock:
            if self.status == "running":
                self.result_status, self.message = "done", "演示任务已结束，未执行任何真实学习操作。"

    def stop(self, reason="stopped", expected_job=None):
        with self.lock:
            if self.status != "running" or (expected_job is not None and expected_job != self.job_id):
                return
            self.status, self.message, self.qr = reason, ("已达到本次总时长，任务已结束。进度以平台最近一次接收为准。" if reason == "limited" else "任务已停止。已提交的平台进度会保留。"), None
            self.ended_at = time.time()
            process = self.process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for course in self.courses:
                if course["status"] in ("running", "queued"):
                    course["status"] = reason
            self.log(self.message, "warning")
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=4)

    def close(self):
        self.closed.set()
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=2)
        self.stop()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
        self.runtime.cleanup()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_content(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.valid_host():
            self.send_content(403, {"error": "仅允许本机访问。"})
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            self.send_content(200, self.server.controller.state())
            return
        allowed = {"/": ("index.html", "text/html; charset=utf-8"),
                   "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                   "/features.js": ("features.js", "text/javascript; charset=utf-8"),
                   "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                   "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path not in allowed:
            self.send_content(404, {"error": "页面不存在。"})
            return
        filename, content_type = allowed[path]
        body = (ASSETS / filename).read_bytes()
        if filename == "index.html":
            body = body.replace(b"<!-- KILLZHS_FEATURE_PAGES -->", (ASSETS / "features.html").read_bytes())
        self.send_content(200, body, content_type)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.valid_host() or (origin and origin not in (
                f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")):
            self.send_content(403, {"error": "请求来源不正确。"})
            return
        if not secrets.compare_digest(self.headers.get("X-Local-Token", ""), self.server.controller.token):
            self.send_content(403, {"error": "页面已失效，请刷新后再试。"})
            return
        if urlsplit(self.path).path != "/api/action":
            self.send_content(404, {"error": "接口不存在。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 65536:
                raise InputError("请求内容过大或为空。")
            data = json.loads(self.rfile.read(length))
            if isinstance(data, dict) and data.get("action") == "quit":
                self.send_content(200, {"ok": True})
                self.server.closing.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.server.controller.dispatch(data)
            self.send_content(200, {"ok": True})
        except (ValueError, TypeError) as error:
            self.send_content(400, {"error": str(error) if isinstance(error, InputError) else "请求格式不正确。"})
        except OSError:
            self.send_content(500, {"error": "无法保存本地文件，请检查目录权限。"})


class InstanceLock:
    """One service per data directory, including across different ports."""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / "instance.lock").open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()


def desktop_window(server, url):
    import tkinter as tk
    from tkinter import ttk
    window = tk.Tk()
    window.title("KillZHS")
    window.geometry("390x220")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="KillZHS 正在运行", font=("Microsoft YaHei UI", 16)).pack(anchor="w")
    ttk.Label(frame, text="在浏览器中管理课程、AI 和定时计划。").pack(anchor="w", pady=(10, 4))
    status = ttk.Label(frame, text="关闭网页后，后台计划仍可运行。")
    status.pack(anchor="w", pady=(0, 16))
    ttk.Button(frame, text="打开工作台", command=lambda: webbrowser.open(url)).pack(side="left")

    def quit_app():
        server.closing.set()
        window.destroy()

    ttk.Button(frame, text="退出并停止任务", command=quit_app).pack(side="right")
    window.protocol("WM_DELETE_WINDOW", quit_app)

    def tick():
        if server.closing.is_set():
            window.destroy()
            return
        with server.controller.lock:
            status.configure(text=server.controller.message[:35])
        window.after(1000, tick)
    window.after(1000, tick)
    window.after(500, window.iconify)
    window.mainloop()


def main():
    parser = argparse.ArgumentParser(description="KillZHS 本地学习工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="仅使用本地演示数据，不连接平台")
    parser.add_argument("--data-dir", type=Path, help="独立的用户数据目录")
    parser.add_argument("--desktop", action="store_true", help="显示后台控制窗口")
    parser.add_argument("--headless", action="store_true", help="以服务模式运行")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    frozen = getattr(sys, "frozen", False)
    data_dir = args.data_dir or ((Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KillZHS") if frozen else ROOT / ".local" / "webui")
    if args.demo and args.data_dir is None:
        data_dir = Path(tempfile.gettempdir()) / "killzhs-demo"
    try:
        instance = InstanceLock(data_dir)
    except OSError:
        metadata = load_json(data_dir / "instance.json", {})
        port = metadata.get("port", args.port)
        if type(port) is int and 1 <= port <= 65535 and not args.no_browser:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as response:
                    if json.load(response).get("app") == "KillZHS":
                        webbrowser.open(f"http://127.0.0.1:{port}")
                        return 0
            except Exception:
                pass
        print("KillZHS 已在运行，或用户目录暂时无法使用。")
        return 1
    controller = Controller(demo=args.demo, data_dir=data_dir)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        controller.close()
        instance.close()
        print(f"端口 {args.port} 无法使用，请换一个端口：python webui.py --port 8766")
        return 1
    server.controller = controller
    server.closing = threading.Event()
    atomic_json(data_dir / "instance.json", {"port": args.port, "pid": os.getpid()})
    controller.start_scheduler()
    url = f"http://127.0.0.1:{args.port}"
    print(f"KillZHS 已启动：{url}" + ("（演示模式）" if args.demo else ""))
    print("保持此窗口打开。按 Ctrl+C 退出并停止任务。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        if (args.desktop or frozen) and not args.headless:
            server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
            server_thread.start()
            try:
                desktop_window(server, url)
            finally:
                server.closing.set()
                server.shutdown()
                server_thread.join(timeout=5)
        else:
            server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在关闭工作台…")
    finally:
        server.server_close()
        controller.close()
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
