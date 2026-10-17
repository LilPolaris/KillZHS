"""Loopback-only HTTP endpoints and a fixed public asset allowlist."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..models import InputError

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-src https://*.zhihuishu.com; frame-ancestors 'none'; base-uri 'none'")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller, assets):
        if address[0] != "127.0.0.1":
            raise ValueError("仅允许监听 127.0.0.1。")
        super().__init__(address, Handler)
        self.controller = controller
        self.closing = threading.Event()
        assets = Path(assets)
        self.assets = {
            "/": (assets / "index.html", "text/html; charset=utf-8"),
            "/styles.css": (assets / "styles.css", "text/css; charset=utf-8"),
            "/favicon.svg": (assets / "favicon.svg", "image/svg+xml"),
        }
        self.assets.update({"/js/" + path.name: (path, "text/javascript; charset=utf-8")
                            for path in (assets / "js").glob("*.js") if path.is_file()})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send_content(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        for key, value in {
            "Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Content-Security-Policy": CSP,
        }.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get("Host") in (
            f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}",
        )

    def valid_token(self):
        value = self.headers.get("X-Local-Token", "")
        return value.isascii() and secrets.compare_digest(value, self.server.controller.token)

    def do_GET(self):
        if not self.valid_host():
            self.send_content(403, {"error": "仅允许本机访问。"})
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            try:
                since = int(query["since"][0]) if "since" in query else None
                if since is not None and since < 0:
                    raise ValueError
            except (ValueError, IndexError):
                self.send_content(400, {"error": "日志游标格式不正确。"})
                return
            self.send_content(200, self.server.controller.state(since, query.get("history_revision", [None])[0]))
            return
        if parsed.path.startswith("/api/task-log/"):
            if not self.valid_token():
                self.send_content(403, {"error": "页面已失效，请刷新后再试。"})
                return
            try:
                result = self.server.controller.task_log(parsed.path.removeprefix("/api/task-log/"))
            except (InputError, FileNotFoundError):
                self.send_content(404, {"error": "任务记录不存在。"})
            else:
                self.send_content(200, result)
            return
        asset = self.server.assets.get(parsed.path)
        if asset is None:
            self.send_content(404, {"error": "页面不存在。"})
            return
        try:
            body = asset[0].read_bytes()
        except OSError:
            self.send_content(404, {"error": "页面不存在。"})
            return
        self.send_content(200, body, asset[1])

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.valid_host() or (origin and origin not in (
            f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}",
        )):
            self.send_content(403, {"error": "请求来源不正确。"})
            return
        if not self.valid_token():
            self.send_content(403, {"error": "页面已失效，请刷新后再试。"})
            return
        path = urlsplit(self.path).path
        name = path.removeprefix("/api/actions/")
        if not path.startswith("/api/actions/") or not name or "/" in name:
            self.send_content(404, {"error": "接口不存在。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 65536 or self.headers.get("Transfer-Encoding"):
                # Consume a bounded body before closing the socket. On Windows,
                # an unread body can reset TCP and hide the intended 400 reply.
                if length > 65536 and not self.headers.get("Transfer-Encoding"):
                    self.rfile.read(min(length, 65537))
                raise InputError("请求内容过大或为空。")
            data = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict):
                raise InputError("请求格式不正确。")
            if name == "quit":
                self.send_content(200, {"ok": True})
                self.server.closing.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.server.controller.action_request(name, data)
            self.send_content(200, {"ok": True})
        except (ValueError, TypeError) as error:
            self.send_content(400, {"error": str(error) if isinstance(error, InputError) else "请求格式不正确。"})
        except OSError:
            self.send_content(500, {"error": "无法保存本地文件，请检查目录权限。"})
