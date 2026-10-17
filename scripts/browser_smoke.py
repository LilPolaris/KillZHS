"""Offline browser -> API -> task archive smoke test on an isolated demo server."""

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from webui import Controller, Handler


def main():
    output = ROOT / "output" / "playwright" / "smoke"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="killzhs-browser-") as directory:
        controller = Controller(root=directory, demo=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.controller, server.closing = controller, threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    page.wait_for_function("() => connected && state.demo")
                    page.locator("#select-all").check()
                    page.locator("#start").click()
                    page.wait_for_function("() => state.status === 'running'")
                    page.locator("a[data-view=plan]").click()
                    page.locator("#plan-catchup").fill("120")
                    page.locator("#plan-form button[type=submit]").click()
                    page.wait_for_function("() => state.plan.catchup_minutes === 120 && !pending")
                    page.locator("a[data-view=overview]").click()
                    page.locator("#stop").click()
                    page.wait_for_function("() => state.status === 'stopped'", timeout=10000)
                    page.locator("a[data-view=logs]").click()
                    page.locator("#task-history [data-task-log]").first.click()
                    page.wait_for_function("() => !document.getElementById('download-history').disabled")
                    assert "演示操作" in page.locator("#history-log-text").inner_text()
                    with page.expect_download() as download:
                        page.locator("#download-history").click()
                    download.value.save_as(str(output / "demo-task.log"))
                    page.screenshot(path=str(output / "task-log.png"), full_page=True)
                    page.locator("#close-history").click()
                    page.locator("a[data-view=notifications]").click()
                    page.locator("#pushplus-token").fill("offline-browser-test-token")
                    page.locator("#notification-form button[type=submit]").click()
                    page.wait_for_function("() => state.notifications.has_pushplus_token && !pending")
                    assert page.locator("#pushplus-token").input_value() == ""
                    page.locator("#test-notification").click()
                    page.wait_for_function("() => state.logs.some(row => row.message.includes('测试通知已模拟')) && !pending")
                    assert "offline-browser-test-token" not in page.request.get(
                        f"http://127.0.0.1:{server.server_port}/api/state").text()
                    original_demo = controller.run_demo

                    def verification_demo(payload):
                        controller.event({"type": "course", "key": "zhidao:demo01", "status": "done"})
                        controller.event({"type": "course", "key": "zhidao:demo02", "status": "attention"})
                        controller.event({"type": "result", "reason": "verification", "error": True,
                                          "message": "需要手动验证，本批次已停止。"})

                    controller.run_demo = verification_demo
                    page.locator("a[data-view=overview]").click()
                    page.locator("#start").click()
                    page.wait_for_function("() => state.status === 'error' && state.verification")
                    assert page.locator("#verification-panel").is_visible()
                    assert page.locator("#open-verification").get_attribute("href") == (
                        "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=demo02")
                    external = []
                    page.route("https://**/*", lambda route: (external.append(route.request.url), route.abort()))
                    page.locator("#open-verification").click()
                    assert not external
                    page.reload()
                    page.wait_for_function("() => connected && state.verification && selected.size === 3")
                    assert page.locator("#task-status").inner_text() == "等待验证"
                    page.screenshot(path=str(output / "verification-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output / "verification-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    controller.run_demo = original_demo
                    page.locator("#restart-after-verification").click()
                    page.wait_for_function("() => state.status === 'running' && !state.verification")
                    assert len(controller.job_courses) == 3
                    assert all(row["id"] != "demo01" for row in controller.job_courses)
                    page.locator("#stop").click()
                    page.wait_for_function("() => state.status === 'stopped'", timeout=10000)
                    page.locator("a[data-view=notifications]").click()
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.mouse.move(389, 843)
                    page.screenshot(path=str(output / "notifications-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    assert not errors, errors
                except Exception:
                    page.screenshot(path=str(output / "failure.png"), full_page=True)
                    raise
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            controller.close()
    print(json.dumps({"offline_browser_flow": "passed", "page_errors": errors}))


if __name__ == "__main__":
    main()
