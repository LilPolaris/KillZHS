import copy
from datetime import datetime, timedelta, timezone
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import requests
from killzhs_config import AI_DEFAULTS, InputError, next_run, validate_plan
from killzhs_notifications import (Notifier, public_notifications, send_notification,
                                  validate_notifications)
from killzhs_tasklogs import TaskLogs
from webui import Controller, DEFAULTS, Handler, PREFIX, atomic_json, normalize_course
import webui_worker as worker


class NotificationsTest(unittest.TestCase):
    def test_secret_preservation_public_state_and_validation(self):
        config = validate_notifications({"enabled": True, "pushplus_token": "private-token"})
        updated = validate_notifications({"events": {"finished": False}}, config)
        self.assertEqual(updated["pushplus_token"], "private-token")
        self.assertFalse(updated["events"]["finished"])
        self.assertNotIn("private-token", json.dumps(public_notifications(updated)))
        self.assertTrue(public_notifications(updated)["has_pushplus_token"])
        for invalid in ({"enabled": True}, {"events": {"other": True}},
                        {"bark_server": "https://user:password@example.test"},
                        {"bark_server": "https://example.test?key=private"}):
            with self.subTest(invalid=invalid), self.assertRaises(InputError):
                validate_notifications(invalid)

    def test_provider_json_payloads_and_business_errors(self):
        config = validate_notifications({"enabled": True, "pushplus_token": "private-token",
                                         "bark_key": "private-device"})
        opener = unittest.mock.MagicMock()
        for provider in ("pushplus", "bark"):
            config["provider"] = provider
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"code":200}'
            opener.open.return_value = response
            with patch("killzhs_notifications.build_opener", return_value=opener):
                send_notification(config, "title", "body")
            request = opener.open.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.method, "POST")
            self.assertNotIn("private", request.full_url)
            self.assertEqual(payload["token" if provider == "pushplus" else "device_key"],
                             "private-token" if provider == "pushplus" else "private-device")
        response.__enter__.return_value.read.return_value = b'{"code":400}'
        with patch("killzhs_notifications.build_opener", return_value=opener), self.assertRaises(ValueError):
            send_notification(config, "title", "body")

    def test_delivery_does_not_block_and_failures_are_redacted(self):
        entered, release, reported = threading.Event(), threading.Event(), threading.Event()
        messages = []
        def sender(*_):
            entered.set()
            release.wait(2)
            raise RuntimeError("private-token https://secret.test/private")
        def report(message, level):
            messages.append(message)
            reported.set()
        notifier = Notifier(report, sender=sender)
        try:
            config = validate_notifications({"enabled": True, "pushplus_token": "private-token"})
            started = time.monotonic()
            self.assertTrue(notifier.submit(config, "finished", "title", "body"))
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.wait(2))
            release.set()
            self.assertTrue(reported.wait(2))
            self.assertNotIn("private-token", str(messages))
            self.assertIn("失败", messages[0])
            config["events"]["finished"] = False
            self.assertFalse(notifier.submit(config, "finished", "title", "body"))
        finally:
            release.set()
            notifier.close()


class TaskLogsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = Controller(self.root)
        self.controller.courses = [normalize_course({"id": "12", "kind": "hike", "name": "course"})]
        self.controller.logged_in = True

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def fake_worker(self, source):
        path = self.root / "fake_worker.py"
        path.write_text("import sys,time,json\nsys.stdin.readline()\n" + source, encoding="utf-8")
        self.controller.worker_command = [sys.executable, "-u", str(path)]

    def test_task_logs_survive_restart_and_never_include_saved_secrets(self):
        self.controller.dispatch({"action": "ai_settings", "ai": {"openai": {"api_key": "private-ai-key"}}})
        self.fake_worker("print(" + repr(PREFIX + json.dumps({"type": "log", "message": "failure private-ai-key"})) + ")\n")
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        self.controller.thread.join(5)
        self.assertFalse(self.controller.thread.is_alive())
        row = self.controller.history[-1]
        self.assertTrue(row["has_log"])
        text = self.controller.task_log(row["id"])["text"]
        self.assertIn("[已隐藏]", text)
        self.assertNotIn("private-ai-key", text)
        self.assertFalse((self.controller.local / "active-task.json").exists())
        restored = Controller(self.root)
        try:
            self.assertEqual(restored.task_log(row["id"])["text"], text)
        finally:
            restored.close()

    def test_unexpected_exit_is_recovered_as_interrupted(self):
        task_id = "a" * 32
        self.controller.task_logs.append(task_id, {"time": "2026-01-01", "level": "info", "message": "before crash"})
        atomic_json(self.controller.local / "active-task.json", {
            "id": task_id, "started_at": time.time() - 10, "action": "start", "origin": "schedule",
            "status": "running", "courses": ["course"], "ai_requests": 0})
        restored = Controller(self.root)
        try:
            self.assertEqual(restored.history[-1]["status"], "interrupted")
            self.assertIn("before crash", restored.task_log(task_id)["text"])
            self.assertFalse((restored.local / "active-task.json").exists())
        finally:
            restored.close()

    def test_log_cap_retention_and_path_validation(self):
        logs = TaskLogs(self.root)
        row = {"time": "now", "level": "info", "message": "x" * 1500}
        with patch("killzhs_tasklogs.MAX_LOG_BYTES", 4096):
            for _ in range(10):
                logs.append("a" * 32, row)
            self.assertLessEqual(logs.path("a" * 32).stat().st_size, 4096)
            self.assertTrue(logs.read("a" * 32)["truncated"])
        logs.append("b" * 32, row)
        unrelated = logs.directory / "manual.log"
        unrelated.write_text("keep")
        logs.prune({"b" * 32})
        self.assertFalse(logs.path("a" * 32).exists())
        self.assertTrue(unrelated.exists())
        for value in ("../ai.json", "..", "", "invalid"):
            with self.assertRaises(ValueError):
                logs.path(value)

    def test_history_endpoint_requires_token_and_known_task_id(self):
        task_id = "a" * 32
        self.controller.history.append({"id": task_id})
        self.controller.task_logs.append(task_id, {"time": "now", "level": "info", "message": "saved entry"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.controller = self.controller
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/task-log/" + task_id)
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request("GET", "/api/task-log/" + task_id,
                               headers={"X-Local-Token": self.controller.token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("saved entry", json.loads(response.read())["text"])
            connection.request("GET", "/api/task-log/../ai.json",
                               headers={"X-Local-Token": self.controller.token})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_catchup_window_is_opt_in_and_attempt_survives_restart(self):
        now = datetime(2026, 10, 19, 21, 0, tzinfo=timezone(timedelta(hours=8)))
        self.controller.plan = validate_plan({"enabled": True, "time": "20:00",
            "catchup_minutes": 120, "courses": ["hike:12"]}, {"hike:12"})
        self.assertEqual(next_run(self.controller.plan, now=now), now.isoformat())
        self.fake_worker("")
        self.controller.schedule_tick(now)
        self.controller.thread.join(5)
        self.assertEqual(self.controller.history[-1]["origin"], "schedule")
        self.assertEqual(self.controller.history[-1]["status"], "done")
        restored = Controller(self.root)
        try:
            self.assertEqual(restored.last_schedule_date, now.date().isoformat())
            restored.plan = copy.deepcopy(self.controller.plan)
            restored.schedule_tick(now)
            self.assertEqual(len(restored.history), 1)
        finally:
            restored.close()
        self.controller.last_schedule_date = None
        self.controller.plan["catchup_minutes"] = 0
        self.controller.schedule_tick(now)
        self.assertEqual(self.controller.history[-1]["status"], "skipped")

    def test_plan_can_be_paused_without_stopping_current_task(self):
        self.fake_worker("time.sleep(60)\n")
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        self.controller.dispatch({"action": "plan", "plan": {"enabled": True, "courses": ["hike:12"], "catchup_minutes": 30}})
        self.controller.dispatch({"action": "pause_plan"})
        self.assertEqual(self.controller.status, "running")
        self.assertFalse(self.controller.plan["enabled"])

    def test_bad_sync_row_preserves_good_and_previous_courses(self):
        self.controller.event({"type": "courses", "kinds": ["hike"], "courses": [
            {"id": "34", "kind": "hike"}, {"id": "bad id", "kind": "hike"}]})
        self.assertEqual({c["id"] for c in self.controller.courses}, {"12", "34"})

    def test_finished_and_skipped_events_are_queued_from_controller(self):
        self.controller.notifications = validate_notifications({"enabled": True, "pushplus_token": "test-token"})
        self.fake_worker("")
        with patch.object(self.controller.notifier, "submit") as submit:
            self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
            self.controller.thread.join(5)
            self.assertEqual(submit.call_args.args[1], "finished")
            self.controller.plan = validate_plan({"enabled": True, "time": "20:00", "courses": ["hike:12"]}, {"hike:12"})
            now = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc)
            self.controller.schedule_tick(now)
            self.assertEqual(submit.call_args.args[1], "skipped")

    def test_notification_result_is_saved_to_its_original_task(self):
        entered, release = threading.Event(), threading.Event()
        def sender(*_):
            entered.set()
            release.wait(3)
        self.controller.notifier.sender = sender
        self.controller.notifications = validate_notifications({"enabled": True, "pushplus_token": "test-token"})
        self.fake_worker("")
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        self.controller.thread.join(5)
        first_id = self.controller.history[-1]["id"]
        self.assertTrue(entered.wait(2))
        self.fake_worker("time.sleep(60)\n")
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        second_id = self.controller.task_id
        release.set()
        self.controller.notifier.queue.join()
        self.assertIn("已提交到推送服务", self.controller.task_log(first_id)["text"])
        self.assertNotIn("已提交到推送服务", self.controller.task_log(second_id)["text"])
        self.controller.notification_report("skip result", "info", None)


class WorkerBoundariesTest(unittest.TestCase):
    def test_captcha_stops_batch_and_bad_sync_rows_do_not_discard_good_rows(self):
        logger = logging.Logger("offline-captcha", logging.INFO)
        calls = []
        class Client:
            uuid = "test-uuid"
            cookies = []
            def __init__(self, **_): pass
            def getZhidaoList(self):
                return [{"secret": "good", "courseName": "good"}, {"courseName": "bad"}]
            def getHikeList(self): return []
            def fuckZhidaoCourse(self, identifier):
                calls.append(identifier)
                logger.info("Captcha required")
        modules = {"utils": types.SimpleNamespace(getRealPath=lambda value: value, cookie_jar_to_list=lambda value: value),
                   "logger": types.SimpleNamespace(logger=logger), "fucker": types.SimpleNamespace(Fucker=Client)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cookies.json").write_text('[{"value":"private-cookie"}]', encoding="utf-8")
            payload = {"root": directory, "local": directory, "runtime": directory, "settings": DEFAULTS.copy(),
                       "ai": copy.deepcopy(AI_DEFAULTS), "action": "refresh", "courses": []}
            output = io.StringIO()
            worker.ISSUES.clear()
            with patch.dict(sys.modules, modules), patch.object(worker, "OUTPUT", output), \
                    patch.object(requests.sessions.Session, "request", side_effect=AssertionError("NETWORK_FORBIDDEN")):
                worker.run(payload)
                events = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
                courses = next(event for event in events if event["type"] == "courses")
                self.assertEqual([row["id"] for row in courses["courses"]], ["good"])
                self.assertEqual(courses["incomplete_kinds"], ["zhidao"])
                payload.update(action="start", courses=[
                    {"id": "first", "kind": "zhidao", "name": "first"},
                    {"id": "second", "kind": "zhidao", "name": "second"}])
                output.seek(0)
                output.truncate()
                worker.run(payload)
                self.assertEqual(calls, ["first"])
                self.assertIn('"reason": "verification"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
