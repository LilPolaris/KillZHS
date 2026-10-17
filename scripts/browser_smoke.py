"""Offline browser smoke, with an optional real local demo server.

Default: isolated in-memory HTTP fixture, no imports or execution of legacy code.
--url: exercise the new backend's demo mode instead (must already be running).
Artifacts and a JSON report go to output/playwright/smoke. No installation needed.
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ("overview", "logs", "settings", "ai", "assessments", "official", "plan", "notifications", "guide")
NOW = "2026-10-17T04:00:00+00:00"


def demo_state():
    """Public-state contract shared by both frontends, without secret values."""
    settings = {"speed": None, "limit": 0, "threshold": 0.91, "remember": True, "budget_minutes": 0}
    return {
        "token": "offline-browser-token",
        "demo": True,
        "status": "idle",
        "action": "",
        "logged_in": True,
        "saved_session": True,
        "qr": None,
        "progress": None,
        "current": None,
        "message": "演示模式 · 所有操作均为本地模拟。",
        "started_at": None,
        "ended_at": None,
        "settings": settings,
        "defaults": copy.deepcopy(settings),
        "missing_dependencies": [],
        "courses": [
            {"kind": kind, "id": ident, "name": name, "status": "ready"}
            for kind, ident, name in (
                ("zhidao", "demo01", "艺术与审美"),
                ("zhidao", "demo02", "大学生心理健康"),
                ("hike", "demo03", "大学英语"),
            )
        ],
        "logs": [{"id": 1, "time": NOW, "level": "info", "message": "本地工作台已就绪。 当前为演示模式。"}],
        "log_cursor": 1,
        "logs_reset": True,
        "version": "0.2.0",
        "app": "KillZHS",
        "ai": {
            "enabled": False,
            "no_exam": True,
            "use_zhidao_ai": True,
            "use_stream": True,
            "max_requests": 10,
            "openai": {"api_base": "https://api.openai.com/v1", "model_name": "", "has_api_key": False},
            "ppt_processing": {
                "provide_to_ai": False,
                "moonShot": {
                    "base_url": "https://api.moonshot.cn/v1",
                    "delete_after_convert": True,
                    "has_api_key": False,
                },
            },
        },
        "ai_requests": 0,
        "active_ai_max_requests": None,
        "plan": {
            "enabled": False,
            "time": "20:00",
            "weekdays": list(range(7)),
            "minutes": 30,
            "catchup_minutes": 0,
            "courses": [],
        },
        "next_run": None,
        "notifications": {
            "enabled": False,
            "provider": "pushplus",
            "bark_server": "https://api.day.app",
            "has_pushplus_token": False,
            "has_bark_key": False,
            "events": dict.fromkeys(("finished", "verification", "auth", "skipped"), True),
        },
        "history": [],
        "history_revision": "offline-0",
        "deadline": None,
        "stop_requested": False,
        "verification": None,
        "verification_probe": None,
        "verify_probe_enabled": False,
        "verify_probe_available": False,
        "assessments": [],
        "assessment_results": {},
    }


def key_of(course):
    return f"{course['kind']}:{course['id']}" + (f":{course['class_id']}" if course["kind"] == "ai" else "")


def assessment_key(row):
    return f"{row['course_key']}:{row['kind']}:{row['id']}:{row['exam_id']}"


class Fixture:
    """Only deterministic UI responses; deliberately does not emulate a platform."""

    def __init__(self):
        self.state = demo_state()
        self.posts = []
        self.queries = []
        self.grade_rows = None
        self.lock = threading.RLock()

    def public_state(self, query):
        with self.lock:
            self.queries.append(query)
            value = copy.deepcopy(self.state)
            since = int(query["since"][0]) if "since" in query else None
            reset = (
                since is None or since > value["log_cursor"] or (value["logs"] and since < value["logs"][0]["id"] - 1)
            )
            value["logs_reset"] = bool(reset)
            if not reset:
                value["logs"] = [row for row in value["logs"] if row["id"] > since]
            if query.get("history_revision", [None])[0] == value["history_revision"]:
                value.pop("history")
            return value

    def log(self, message):
        self.state["log_cursor"] += 1
        self.state["logs"].append({"id": self.state["log_cursor"], "time": NOW, "level": "info", "message": message})

    def archive(self, action, status="done"):
        self.state["status"] = status
        self.state["action"] = action
        self.state["ended_at"] = 1792209660
        self.state["history"].insert(
            0,
            {
                "id": "offline-task",
                "action": action,
                "status": status,
                "origin": "manual",
                "message": "演示操作已模拟。",
                "started_at": 1792209600,
                "ended_at": 1792209660,
                "ai_requests": 0,
                "has_log": True,
            },
        )
        self.state["history_revision"] = f"offline-{len(self.state['history'])}"
        self.log("演示操作已模拟。")

    def dispatch(self, name, params):
        with self.lock:
            self.posts.append((name, copy.deepcopy(params)))
            if "action" in params:
                raise ValueError("Action must be in the URL, never in the body")
            state = self.state
            if name in ("settings", "plan"):
                state[name] = params[name]
            elif name == "ai_settings":
                value = copy.deepcopy(params["ai"])
                for provider, previous in (
                    (value["openai"], state["ai"]["openai"]),
                    (value["ppt_processing"]["moonShot"], state["ai"]["ppt_processing"]["moonShot"]),
                ):
                    provider["has_api_key"] = (
                        bool(provider.pop("api_key")) if "api_key" in provider else previous["has_api_key"]
                    )
                state["ai"] = value
            elif name == "notification_settings":
                value = copy.deepcopy(params["notifications"])
                for key in ("pushplus_token", "bark_key"):
                    if key in value:
                        value["has_" + key] = bool(value.pop(key))
                state["notifications"].update(value)
            elif name == "test_notification":
                self.log("演示模式：测试通知已模拟，未连接推送服务。")
            elif name == "pause_plan":
                state["plan"]["enabled"] = False
            elif name == "add":
                course = dict(params["course"], status="ready")
                if course["kind"] == "auto":
                    course["kind"] = "zhidao"
                state["courses"].append(course)
            elif name in ("start", "login", "refresh", "test_ai", "diagnose"):
                state.update(
                    status="running",
                    action=name,
                    started_at=1792209600,
                    ended_at=None,
                    message="演示操作正在运行。",
                    verification=None,
                )
                if name != "start":
                    self.archive(name)
            elif name == "stop":
                self.archive(state["action"], "stopped")
            elif name == "assessment_scan":
                course = params["courses"][0]
                state["assessments"] = [
                    {
                        "course_key": course,
                        "kind": kind,
                        "id": str(index),
                        "exam_id": str(index),
                        "state": 1,
                        "name": title,
                        "url": "https://onlineexamh5new.zhihuishu.com/",
                        "end": "",
                    }
                    for index, kind, title in ((1, "chapter", "第一章测验"), (2, "exam", "期末考试"))
                ]
                if self.grade_rows is not None:
                    state["assessments"] = copy.deepcopy(self.grade_rows)
                self.archive(name)
            elif name == "assessment_run":
                state["assessment_results"] = {
                    key: {"state": "submitted", "message": "演示交卷成功"} for key in params["assessments"]
                }
                self.archive(name)
            elif name == "confirm_verification":
                probe = state["verification_probe"]
                assert params == {"task_id": probe["task_id"], "probe_id": probe["probe_id"]}
                probe.update(phase="observed", awaiting_confirmation=False, normal_reports=3)
                state["verification"] = None
                self.archive("start")
            else:
                raise ValueError(f"Unsupported offline action: {name}")

    def verification(self, probe=False):
        with self.lock:
            self.state.update(
                status="running" if probe else "error",
                action="start",
                verify_probe_enabled=probe,
                verification={
                    "task_id": "offline-task",
                    "course_keys": ["zhidao:demo02", "hike:demo03"],
                    "course_key": "zhidao:demo02",
                    "course_name": "大学生心理健康",
                    "direct": True,
                    "url": "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=demo02",
                },
            )
            self.state["verification_probe"] = (
                {
                    "task_id": "offline-task",
                    "probe_id": "offline-probe",
                    "phase": "waiting",
                    "awaiting_confirmation": True,
                    "confirmation_sent": False,
                }
                if probe
                else None
            )


@contextmanager
def offline_site(*, legacy=None, fixture=None):
    """Serve only the whitelisted assets and memory fixture on an ephemeral port."""
    fixture = fixture or Fixture()
    assets = Path(legacy) / "web" if legacy else ROOT / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-src https://*.zhihuishu.com; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/state":
                self.send(200, fixture.public_state(parse_qs(parsed.query)))
                return
            if path.startswith("/api/task-log/"):
                if self.headers.get("X-Local-Token") != fixture.state["token"]:
                    self.send(403, {"error": "页面已失效，请刷新后再试。"})
                else:
                    self.send(200, {"text": "演示操作已模拟。\n本地日志，不连接真实服务。"})
                return
            names = {"/": "index.html", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
            if legacy:
                names.update({"/" + name: name for name in ("app.js", "features.js", "assessments.js")})
            else:
                names.update({"/js/" + item.name: "js/" + item.name for item in (assets / "js").glob("*.js")})
            if path not in names:
                self.send(404, {"error": "页面不存在。"})
                return
            name = names[path]
            body = (assets / name).read_bytes()
            if legacy and name == "index.html":
                body = body.replace(b"<!-- KILLZHS_FEATURE_PAGES -->", (assets / "features.html").read_bytes())
                body = body.replace(b"</main>", (assets / "assessments.html").read_bytes() + b"</main>")
            content_type = {
                ".html": "text/html",
                ".css": "text/css",
                ".js": "text/javascript",
                ".svg": "image/svg+xml",
            }[Path(name).suffix]
            self.send(200, body, content_type + "; charset=utf-8")

        def do_POST(self):
            if self.headers.get("X-Local-Token") != fixture.state["token"]:
                self.send(403, {"error": "页面已失效，请刷新后再试。"})
                return
            try:
                path = urlsplit(self.path).path
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if legacy and path == "/api/action":
                    name = params.pop("action")
                elif not legacy and path.startswith("/api/actions/"):
                    name = path.removeprefix("/api/actions/")
                else:
                    self.send(404, {"error": "接口不存在。"})
                    return
                fixture.dispatch(name, params)
                self.send(200, {"ok": True})
            except (ValueError, KeyError) as error:
                self.send(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def local_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("localhost", "127.0.0.1")
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("Only a loopback HTTP demo server is allowed")
    return value.rstrip("/")


def guard_network(context, allowed):
    blocked = []

    def route(request_route):
        url = request_route.request.url
        parsed = urlsplit(url)
        if parsed.scheme in ("data", "about", "blob") or f"{parsed.scheme}://{parsed.netloc}" in allowed:
            request_route.continue_()
        else:
            blocked.append(url)
            request_route.abort()

    context.route("**/*", route)
    return blocked


def ready(page, url):
    page.goto(url)
    page.wait_for_function(
        "() => document.querySelector('#demo-badge').hidden === false && document.querySelector('#offline-banner').hidden && document.querySelector('#connection-label').textContent === '本地演示已连接'"
    )


def navigate(page, view):
    page.locator(f'a.nav-link[data-view="{view}"]').click()
    page.locator(f"#view-{view}").wait_for(state="visible")


def submit(page, action, click):
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/actions/" + action) as response:
        click()
    assert response.value.ok, response.value.text()
    page.wait_for_function(
        "() => [...document.querySelectorAll('[data-feature-button][type=submit]')].every(button => !button.disabled)"
    )


def smoke(url, output, fixture=None):
    from playwright.sync_api import expect, sync_playwright

    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, accept_downloads=True, locale="zh-CN", timezone_id="Asia/Shanghai"
        )
        blocked = guard_network(context, {url})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            ready(page, url)
            assert page.evaluate("typeof window.state === 'undefined' && typeof window.pending === 'undefined'")
            for view in VIEWS:
                navigate(page, view)
            navigate(page, "overview")
            page.locator("#select-all").check()
            page.locator("#start").click()
            expect(page.locator("#task-status")).to_have_text("正在学习")
            navigate(page, "plan")
            page.locator("#plan-catchup").fill("120")
            submit(page, "plan", lambda: page.locator('#plan-form button[type="submit"]').click())
            expect(page.locator("#plan-catchup")).to_have_value("120")
            navigate(page, "overview")
            page.locator("#stop").click()
            expect(page.locator("#task-status")).to_have_text("已停止", timeout=10000)
            navigate(page, "logs")
            page.locator("#task-history [data-task-log]").first.click()
            expect(page.locator("#download-history")).to_be_enabled()
            expect(page.locator("#history-log-text")).to_contain_text("演示")
            with page.expect_download() as download:
                page.locator("#download-history").click()
            download.value.save_as(str(output / "demo-task.log"))
            page.screenshot(path=str(output / "task-log.png"), full_page=True)
            page.locator("#close-history").click()
            navigate(page, "notifications")
            secret = "offline-browser-test-token"
            page.locator("#pushplus-token").fill(secret)
            submit(
                page, "notification_settings", lambda: page.locator('#notification-form button[type="submit"]').click()
            )
            expect(page.locator("#pushplus-token")).to_have_value("")
            expect(page.locator("#pushplus-key-state")).to_contain_text("已保存在本机")
            assert secret not in page.request.get(url + "/api/state").text()
            submit(page, "test_notification", lambda: page.locator("#test-notification").click())
            # A poll must preserve local edits, and mirrored settings must agree.
            navigate(page, "settings")
            page.locator("#setting-limit").fill("17")
            page.wait_for_timeout(1200)
            expect(page.locator("#setting-limit")).to_have_value("17")
            submit(page, "settings", lambda: page.locator('#settings-form button[type="submit"]').click())
            navigate(page, "ai")
            page.locator("#ai-enabled").check()
            submit(page, "ai_settings", lambda: page.locator('#ai-form button[type="submit"]').click())
            navigate(page, "assessments")
            submit(page, "assessment_scan", lambda: page.locator("#assessment-scan").click())
            expect(page.locator("[data-assessment]")).to_have_count(2)
            page.locator("[data-assessment]").first.check()
            submit(page, "assessment_run", lambda: page.locator("#assessment-run").click())
            expect(page.locator("#assessment-list")).to_contain_text("演示交卷成功", timeout=10000)
            if fixture:
                original_rows = copy.deepcopy(fixture.state["assessments"])
                fixture.grade_rows = [
                    {**original_rows[0], "state": 4, "score": 0, "total_score": 100},
                    {**original_rows[1], "state": 3, "score": 0},
                    {**original_rows[0], "id": "grade3", "name": "已提交，尚未公布分数", "state": 4},
                    {**original_rows[0], "id": "grade4", "name": "小数成绩", "state": 4,
                     "score": 87.5, "total_score": 100},
                    {**original_rows[1], "id": "grade5", "name": "百分制成绩", "state": 4,
                     "score_percent": 88.125},
                ]
                # A fresh platform grade supersedes an older local error.
                fixture.state["assessment_results"][assessment_key(original_rows[0])] = {
                    "state": "attention", "message": "过期的本地错误"}
                navigate(page, "ai")
                page.locator("#ai-enabled").uncheck()
                submit(page, "ai_settings", lambda: page.locator('#ai-form button[type="submit"]').click())
                navigate(page, "assessments")
                before_posts = len(fixture.posts)
                submit(page, "assessment_scan", lambda: page.locator("#assessment-refresh-grades").click())
                expect(page.locator("[data-assessment-grade]").first).to_have_text("成绩：0 / 100 分")
                expect(page.locator("[data-assessment-grade]").nth(1)).to_have_text("成绩：待批阅")
                expect(page.locator("[data-assessment-grade]").nth(2)).to_have_text("成绩：成绩未公布")
                expect(page.locator("[data-assessment-grade]").nth(3)).to_have_text("成绩：87.5 / 100 分")
                expect(page.locator("[data-assessment-grade]").nth(4)).to_have_text("成绩：百分制 88.125 分")
                expect(page.locator("#assessment-list")).not_to_contain_text("过期的本地错误")
                expect(page.locator("[data-assessment]").first).to_be_disabled()
                expect(page.locator("[data-assessment]").nth(1)).to_be_disabled()
                assert fixture.posts[before_posts:] == [("assessment_scan", {"courses": [original_rows[0]["course_key"]]})]
                assert fixture.state["ai"]["enabled"] is False
                page.screenshot(path=str(output / "grades-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Grade view overflows mobile"
                page.screenshot(path=str(output / "grades-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("[data-open-assessment]").first.click()
            expect(page.locator("#official-frame")).to_be_visible()
            assert page.locator("#official-frame").get_attribute("src") is None
            page.locator("#official-return").click()
            expect(page.locator("#official-frame")).to_be_hidden()
            if fixture:
                fixture.verification()
                page.reload()
                ready(page, url)
                navigate(page, "overview")
                expect(page.locator("#task-status")).to_have_text("等待验证")
                expect(page.locator("#verification-panel")).to_be_visible()
                page.locator("#open-verification").click()
                assert not blocked, blocked
                page.screenshot(path=str(output / "verification.png"), full_page=True)
                page.locator("#restart-after-verification").click()
                expect(page.locator("#task-status")).to_have_text("正在学习")
                assert fixture.posts[-1] == ("start", {"courses": ["zhidao:demo02", "hike:demo03"]})
                fixture.verification(probe=True)
                page.reload()
                ready(page, url)
                expect(page.locator("#task-status")).to_have_text("等待验证（实验）")
                expect(page.locator("#restart-after-verification")).to_be_hidden()
                page.locator("#confirm-verification").click()
                expect(page.locator("#task-status")).to_have_text("任务已结束")
                assert fixture.state["verification_probe"]["normal_reports"] == 3
                assert any("since" in query and "history_revision" in query for query in fixture.queries)
                # Cursor reset, incremental history omission, reconnect and token restart.
                from_store = "async () => { const store = await import('/js/store.js'); await store.fetchState(); }"
                page.evaluate(from_store)
                assert page.locator("#task-history [data-task-log]").count() > 0
                page.route("**/api/state*", lambda route: route.abort())
                page.evaluate(from_store)
                expect(page.locator("#offline-banner")).to_be_visible()
                expect(page.locator("#start")).to_be_disabled()
                page.unroute("**/api/state*")
                page.evaluate(from_store)
                expect(page.locator("#offline-banner")).to_be_hidden()
                navigate(page, "settings")
                page.locator("#setting-limit").fill("29")
                fixture.state["token"] = "offline-restarted-token"
                fixture.state["history_revision"] = "restarted-0"
                fixture.state["history"] = []
                fixture.state["logs"] = [{"id": 1, "time": NOW, "level": "info", "message": "重启后的日志。"}]
                fixture.state["log_cursor"] = 1
                page.evaluate(from_store)
                expect(page.locator("#setting-limit")).to_have_value("17")
                expect(page.locator("#selected-count")).to_have_text("0")
                expect(page.locator("#full-logs")).to_contain_text("重启后的日志")
            for width in (1440, 960, 500):
                page.set_viewport_size({"width": width, "height": 1000})
                for view in VIEWS:
                    navigate(page, view)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (view, width)
                page.screenshot(path=str(output / f"guide-{width}.png"), full_page=True)
            assert not blocked, blocked
            assert not errors, errors
        except Exception:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            (output / "errors.json").write_text(
                json.dumps({"page_errors": errors, "blocked_external_requests": blocked}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            raise
        finally:
            context.close()
            browser.close()
    return {
        "offline_browser_flow": "passed",
        "mode": "fixture" if fixture else "demo-server",
        "page_errors": errors,
        "blocked_external_requests": blocked,
        "viewports": [1440, 960, 500],
        "views": list(VIEWS),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=local_url, help="Existing new backend DEMO server (loopback only)")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "playwright" / "smoke")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = args.output / "report.json"
    report.write_text(json.dumps({"offline_browser_flow": "running"}), encoding="utf-8")
    if args.url:
        # Verify demo before any mutation; never direct browser actions at real accounts.
        from urllib.request import urlopen

        with urlopen(args.url + "/api/state", timeout=5) as response:
            if not json.load(response).get("demo"):
                parser.error("The supplied local server must be in demo mode")
        result = smoke(args.url, args.output)
    else:
        with offline_site() as (url, fixture):
            result = smoke(url, args.output, fixture)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
