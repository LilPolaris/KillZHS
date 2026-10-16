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
import webbrowser

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "web"
PREFIX = "@@ZHS_WEB@@"
DEFAULTS = {"speed": None, "limit": 0, "threshold": 0.91, "remember": True}
MODULES = {"requests": "requests", "Pillow": "PIL", "pycryptodome": "Crypto",
           "OpenAI": "openai", "tiktoken": "tiktoken"}


class InputError(ValueError):
    pass


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
    for key, low, high in (("speed", 0.1, 16), ("limit", 0, 1440), ("threshold", 0.1, 2)):
        number = result[key]
        if key == "speed" and number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise InputError("请输入有效的数字。")
        if not low <= number <= high or (key == "limit" and int(number) != number):
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
    if kind not in ("auto", "hike", "zhidao"):
        raise InputError("课程类型不正确。")
    return {"id": identifier, "name": str(row.get("name") or f"课程 {identifier}")[:160],
            "kind": kind, "status": "ready"}


def course_key(course):
    return course["kind"] + ":" + course["id"]


class Controller:
    def __init__(self, root=ROOT, demo=False, worker_command=None):
        self.root = Path(root)
        self.local = self.root / ".local" / "webui"
        self.demo = demo
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.worker_command = worker_command
        self.token = secrets.token_urlsafe(32)
        self.runtime = tempfile.TemporaryDirectory(prefix="zhs-web-")
        stored = load_json(self.local / "settings.json", {})
        try:
            self.settings = validate_settings({k: v for k, v in stored.items() if k in DEFAULTS}) if isinstance(stored, dict) else DEFAULTS.copy()
        except InputError:
            self.settings = DEFAULTS.copy()
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
                "saved_session": (self.local / "cookies.json").is_file() or (self.root / "cookies.json").is_file()})

    def dispatch(self, data):
        if not isinstance(data, dict):
            raise InputError("请求必须为 JSON 对象。")
        action = data.get("action")
        if action == "stop":
            self.stop()
            return
        with self.lock:
            if self.status == "running":
                raise InputError("已有任务正在运行，请等待结束或先停止。")
            if action == "settings":
                self.settings = validate_settings(data.get("settings"))
                if not self.demo:
                    atomic_json(self.local / "settings.json", self.settings)
                    if not self.settings["remember"]:
                        (self.local / "cookies.json").unlink(missing_ok=True)
                self.log("运行设置已保存。")
                return
            if action == "add":
                course = normalize_course(data.get("course"))
                if course_key(course) in {course_key(c) for c in self.courses}:
                    raise InputError("这门课程已经在清单中了。")
                self.courses.append(course)
                self.persist_courses()
                return
            if action not in ("login", "refresh", "start"):
                raise InputError("不支持的操作。")
            if self.dependencies and not self.demo:
                raise InputError("当前 Python 环境缺少依赖：" + "、".join(self.dependencies) + "。请用项目虚拟环境启动。")
            selected = data.get("courses", [])
            if action == "start":
                if not self.logged_in:
                    raise InputError("请先登录并同步课程。")
                if not isinstance(selected, list) or not selected or len(selected) > 200:
                    raise InputError("请至少选择一门课程。")
                known = {course_key(c): c for c in self.courses}
                if any(not isinstance(key, str) or key not in known for key in selected):
                    raise InputError("课程清单已变化，请重新选择。")
                selected = [copy.deepcopy(known[key]) for key in dict.fromkeys(selected)]
            else:
                selected = []
            self.status, self.action = "running", action
            self.result_status, self.ended_at = None, None
            self.qr, self.progress, self.current = None, None, None
            self.started_at = time.time()
            self.message = {"login": "正在连接登录服务…", "refresh": "正在同步课程…", "start": "正在准备学习任务…"}[action]
            for course in self.courses:
                if action == "start":
                    course["status"] = "ready"
                if course_key(course) in {course_key(c) for c in selected}:
                    course["status"] = "queued"
            payload = {"action": action, "root": str(self.root), "local": str(self.local),
                       "runtime": self.runtime.name, "settings": self.settings, "courses": selected}
            self.thread = threading.Thread(target=self.run_job, args=(payload,), daemon=True)
            self.thread.start()

    def persist_courses(self):
        if not self.demo:
            atomic_json(self.local / "courses.json", [{k: c[k] for k in ("id", "name", "kind")} for c in self.courses])

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
            command = self.worker_command or [sys.executable, "-u", str(ROOT / "webui_worker.py")]
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            with self.lock:
                if self.status != "running":
                    return
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    cwd=self.runtime.name, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                process = self.process
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.close()
            for line in process.stdout:
                if line.startswith(PREFIX):
                    with self.lock:
                        if self.status == "stopped":
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
                if self.status != "stopped":
                    self.status, self.message = "error", "无法启动运行进程，请检查 Python 环境。"
                    self.log(self.message, "error")
        finally:
            if process is not None:
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
                        course["status"] = "stopped" if self.status == "stopped" else "attention"

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

    def stop(self):
        with self.lock:
            if self.status != "running":
                return
            self.status, self.message, self.qr = "stopped", "任务已停止。已提交的平台进度会保留。", None
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
                    course["status"] = "stopped"
            self.log("已停止当前任务。", "warning")
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=4)

    def close(self):
        self.stop()
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
                   "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                   "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path not in allowed:
            self.send_content(404, {"error": "页面不存在。"})
            return
        filename, content_type = allowed[path]
        self.send_content(200, (ASSETS / filename).read_bytes(), content_type)

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
            self.server.controller.dispatch(data)
            self.send_content(200, {"ok": True})
        except (ValueError, TypeError) as error:
            self.send_content(400, {"error": str(error) if isinstance(error, InputError) else "请求格式不正确。"})
        except OSError:
            self.send_content(500, {"error": "无法保存本地文件，请检查目录权限。"})


def main():
    parser = argparse.ArgumentParser(description="智慧树本地学习工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="仅使用本地演示数据，不连接平台")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    controller = Controller(demo=args.demo)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        controller.close()
        print(f"端口 {args.port} 无法使用，请换一个端口：python webui.py --port 8766")
        return 1
    server.controller = controller
    url = f"http://127.0.0.1:{args.port}"
    print(f"学习工作台已启动：{url}" + ("（演示模式）" if args.demo else ""))
    print("保持此窗口打开。按 Ctrl+C 退出并停止任务。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在关闭工作台…")
    finally:
        server.server_close()
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
