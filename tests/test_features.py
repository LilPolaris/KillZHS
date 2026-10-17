"""Durable scheduling, budgets and AI configuration, using no real accounts."""

from datetime import datetime, timedelta
import json
import copy
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
import types
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from killzhs_config import (AI_DEFAULTS, InputError, api_url, next_run, public_ai, validate_ai, validate_plan)
from webui import Controller, course_key, normalize_course
import webui_worker as worker


class ConfigTest(unittest.TestCase):
    def test_secret_updates_preserve_existing_keys_and_public_state_omits_them(self):
        saved = validate_ai({"openai": {"api_key": "private-model-key"},
                             "ppt_processing": {"moonShot": {"api_key": "private-ppt-key"}}})
        updated = validate_ai({"openai": {"model_name": "example-model"}}, saved)
        self.assertEqual(updated["openai"]["api_key"], "private-model-key")
        public = public_ai(updated)
        self.assertTrue(public["openai"]["has_api_key"])
        self.assertNotIn("private-", json.dumps(public))
        cleared = validate_ai({"openai": {"api_key": ""}}, updated)
        self.assertFalse(public_ai(cleared)["openai"]["has_api_key"])
        self.assertTrue(public_ai(cleared)["ppt_processing"]["moonShot"]["has_api_key"])

    def test_provider_urls_and_generation_limit_are_validated(self):
        self.assertEqual(api_url("https://example.test"), "https://example.test/v1")
        self.assertEqual(api_url("https://example.test/custom/v1/"), "https://example.test/custom/v1")
        for value in ("file:///private", "https://user:key@example.test", "https://example.test?api_key=secret", "http://example.test:bad", "https://example.test\n"):
            with self.subTest(value=value), self.assertRaises(InputError):
                api_url(value)
        for number in (0, True, 101, 1.5):
            with self.assertRaises(InputError):
                validate_ai({"max_requests": number})

    def test_ai_course_keys_include_class_and_require_numeric_ids(self):
        one = normalize_course({"id": "12", "kind": "ai", "class_id": "34"})
        two = normalize_course({"id": "12", "kind": "ai", "class_id": "35"})
        self.assertNotEqual(course_key(one), course_key(two))
        with self.assertRaises(InputError):
            normalize_course({"id": "12", "kind": "ai"})

    def test_plan_validation_and_next_weekday(self):
        with self.assertRaises(InputError):
            validate_plan({"enabled": True}, set())
        with self.assertRaises(InputError):
            validate_plan({"time": "25:00"}, set())
        plan = validate_plan({"enabled": True, "time": "10:00", "weekdays": [0], "courses": ["hike:12"]}, {"hike:12"})
        now = datetime(2026, 10, 19, 9, 0).astimezone()  # Monday.
        self.assertEqual(datetime.fromisoformat(next_run(plan, now=now)).date(), now.date())
        self.assertEqual(datetime.fromisoformat(next_run(plan, now.date().isoformat(), now)).date(), (now + timedelta(days=7)).date())


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.controller = Controller(self.root)
        self.controller.dependencies = []
        self.controller.dispatch({"action": "add", "course": {"id": "12", "kind": "hike", "name": "Test course"}})
        self.now = datetime(2026, 10, 19, 10, 0, 5).astimezone()

    def tearDown(self):
        self.controller.close()
        self.directory.cleanup()

    def fake_worker(self, sleep=0):
        worker = self.root / "worker.py"
        worker.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(" + str(sleep) + ")\n", encoding="utf-8")
        self.controller.worker_command = [sys.executable, "-u", str(worker)]

    def enable(self):
        self.controller.dispatch({"action": "plan", "plan": {"enabled": True, "time": "10:00", "courses": ["hike:12"], "minutes": 30}})

    def wait_for_process(self):
        deadline = time.monotonic() + 3
        while self.controller.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.controller.process)
        return self.controller.process

    def test_due_plan_runs_once_and_persists_attempt_across_restart(self):
        self.fake_worker()
        (self.controller.local / "cookies.json").write_text("[]", encoding="utf-8")
        self.enable()
        self.controller.schedule_tick(self.now)
        self.controller.thread.join(timeout=5)
        self.assertEqual(self.controller.origin, "schedule")
        self.assertAlmostEqual(self.controller.deadline - self.controller.started_at, 1800)
        count = len(self.controller.history)
        self.controller.schedule_tick(self.now + timedelta(seconds=5))
        self.assertEqual(len(self.controller.history), count)
        other = Controller(self.root)
        try:
            self.assertEqual(other.last_schedule_date, self.now.date().isoformat())
            other.schedule_tick(self.now + timedelta(seconds=10))
            self.assertEqual(other.status, "idle")
            self.assertEqual(len(other.history), count)
        finally:
            other.close()

    def test_missing_login_and_missed_times_are_recorded_without_retry(self):
        self.enable()
        self.controller.schedule_tick(self.now)
        self.assertEqual(self.controller.history[-1]["status"], "skipped")
        self.assertIn("登录", self.controller.history[-1]["message"])
        self.controller.schedule_tick(self.now + timedelta(seconds=10))
        self.assertEqual(len(self.controller.history), 1)
        self.controller.schedule_tick(self.now + timedelta(days=1, minutes=5))
        self.assertIn("错过", self.controller.history[-1]["message"])

    def test_expiry_stops_child_and_stale_watchdog_cannot_stop_next_job(self):
        self.fake_worker(sleep=60)
        self.controller.logged_in = True
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        first_id = self.controller.job_id
        process = self.wait_for_process()
        self.controller.expire(first_id)
        self.assertEqual(self.controller.stop_requested, "limited")
        self.controller.stop(reason="limited", expected_job=first_id, force=True)
        self.assertEqual(self.controller.status, "limited")
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.controller.history[-1]["status"], "limited")
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        self.wait_for_process()
        self.controller.expire(first_id)
        self.assertEqual(self.controller.status, "running")
        self.controller.stop()

    def test_background_plan_requires_remember_and_ai_defaults_skip_exams(self):
        self.controller.dispatch({"action": "settings", "settings": {"remember": False}})
        with self.assertRaises(InputError):
            self.enable()
        self.assertFalse(self.controller.ai["enabled"])
        self.assertTrue(self.controller.ai["no_exam"])
        self.controller.dispatch({"action": "ai_settings", "ai": {"openai": {"api_key": "private-key"}}})
        snapshot = json.dumps(self.controller.state())
        self.assertNotIn("private-key", snapshot)

    def test_ai_can_be_saved_during_run_without_changing_active_payload(self):
        capture = self.root / "captured.json"
        script = self.root / "capture_worker.py"
        script.write_text("import json,sys,time\nfrom pathlib import Path\n"
                          "payload=json.loads(sys.stdin.readline())\n"
                          "Path(payload['root'],'captured.json').write_text(json.dumps(payload),encoding='utf-8')\n"
                          "time.sleep(60)\n", encoding="utf-8")
        self.controller.worker_command = [sys.executable, "-u", str(script)]
        self.controller.logged_in = True
        self.controller.dispatch({"action": "start", "courses": ["hike:12"]})
        process = self.wait_for_process()
        started = self.controller.started_at
        deadline = time.monotonic() + 3
        while not capture.is_file() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(capture.is_file())
        self.controller.dispatch({"action": "ai_settings", "ai": {
            "enabled": True, "max_requests": 2, "openai": {"api_key": "offline-test-key"}}})
        snapshot = self.controller.state()
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(self.controller.process.pid, process.pid)
        self.assertIsNone(process.poll())
        self.assertEqual(self.controller.started_at, started)
        self.assertEqual(snapshot["active_ai_max_requests"], 10)
        self.assertEqual(snapshot["ai"]["max_requests"], 2)
        self.assertTrue(snapshot["ai"]["openai"]["has_api_key"])
        self.assertNotIn("offline-test-key", json.dumps(snapshot))
        payload = json.loads(capture.read_text(encoding="utf-8"))
        self.assertFalse(payload["ai"]["enabled"])
        self.assertEqual(payload["ai"]["max_requests"], 10)
        self.assertFalse(payload["ai"]["openai"]["api_key"])
        self.assertEqual(json.loads((self.controller.local / "ai.json").read_text(encoding="utf-8"))["max_requests"], 2)
        with self.assertRaises(InputError):
            self.controller.dispatch({"action": "refresh"})


class AIBudgetTest(unittest.TestCase):
    def test_worker_accepts_pretty_json_from_packaged_file_transport(self):
        payload = {"action": "diagnose", "nested": {"value": "test"}}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload, indent=2))), \
                patch.object(worker, "run") as run, patch.object(worker, "OUTPUT", io.StringIO()):
            self.assertEqual(worker.main(), 0)
            run.assert_called_once_with(payload)

    def test_retry_requests_are_counted_and_cap_escapes_legacy_error_handlers(self):
        import requests
        class Client:
            uuid = "test-uuid"
            def __init__(self, **kwargs):
                self.cookies = []
            def fuckAiCourse(self, *args, **kwargs):
                for _ in range(3):
                    try:
                        requests.post("https://example.test/v1/chat/completions")
                    except Exception:
                        pass
        utils = types.SimpleNamespace(getRealPath=lambda path: path, cookie_jar_to_list=lambda cookies: cookies)
        logger = types.SimpleNamespace(setLevel=lambda _: None, addHandler=lambda _: None)
        modules = {"utils": utils, "logger": types.SimpleNamespace(logger=logger), "fucker": types.SimpleNamespace(Fucker=Client)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cookies.json").write_text('[{"value":"fake-cookie"}]', encoding="utf-8")
            ai = copy.deepcopy(AI_DEFAULTS)
            ai["max_requests"] = 1
            payload = {"root": directory, "local": directory, "runtime": directory,
                "action": "start", "settings": {"speed": None, "limit": 0, "threshold": .91, "remember": True},
                "ai": ai, "courses": [{"id": "12", "class_id": "34", "kind": "ai", "name": "AI test"}]}
            output = io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(worker, "OUTPUT", output), \
                    patch.object(requests.sessions.Session, "request") as request:
                with self.assertRaises(worker.AIBudgetExceeded):
                    worker.run(payload)
                self.assertEqual(request.call_count, 1)
                self.assertIn('"count": 1', output.getvalue())


if __name__ == "__main__":
    unittest.main()
