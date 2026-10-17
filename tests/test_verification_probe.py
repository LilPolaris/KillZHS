import io
import json
import copy
import logging
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import Mock, patch

import requests
from ObjDict import ObjDict
from killzhs_runtime import TaskControl, TaskStopped, finish_on_stop
from killzhs_verification import VerificationProbe
from webui import Controller, InputError, atomic_json, course_key
from killzhs_config import DEFAULTS, AI_DEFAULTS


URL = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"


class Clock:
    def __init__(self):
        self.now = 0
        self.on_sleep = lambda: None

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import utils
        real = utils.getRealPath
        logs = Path(__file__).resolve().parents[1] / ".local/unit-logs"
        logs.mkdir(parents=True, exist_ok=True)
        with patch.object(utils, "getRealPath", side_effect=lambda value: str(logs) if str(value).strip("./") == "logs" else real(value)):
            import fucker
        cls.legacy = fucker

    def prepare(self, *, enabled=True, code=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        client = object.__new__(self.legacy.Fucker)
        client.session = requests.Session()
        self.addCleanup(client.session.close)
        client.proxies = {}
        client.uuid = "private-account"
        client.batch_started_monotonic = time.monotonic() - 5
        client.verification_video = {"course_id": "course", "video_id": 10, "played_time": 12.5}
        client.context = {"course": ObjDict({"course": {"recruitId": 7}, "videos": {10: {"id": 5, "lessonId": 6}}})}
        client.queryStudyInfo = Mock(side_effect=[
            ObjDict({"lv": {"5": {"studyTotalTime": 10, "watchState": 0}}}),
            ObjDict({"lv": {"5": {"studyTotalTime": 25, "watchState": 0}}})])
        client._apiQuery = Mock(return_value=ObjDict({"code": code, "data": {}}))
        client.task_control = TaskControl(root / "stop.json")
        clock, events, logs = Clock(), [], []
        probe = VerificationProbe(client, client.task_control, root / "confirm.json", "a" * 32,
            enabled=enabled, report=lambda **event: events.append(event), log=lambda *row: logs.append(row),
            clock=lambda: clock.now, sleep=clock.sleep, wait_seconds=2)
        client.verification_probe = probe
        probe.course_kind = "zhidao"
        clock.on_sleep = lambda: atomic_json(probe.signal_file, {"task_id": probe.task_id, "probe_id": probe.active})
        return client, probe, clock, events, logs

    def test_default_diagnostics_redact_payload_and_do_not_retry(self):
        client, probe, _, _, _ = self.prepare(enabled=False)
        client.session.cookies.set("session", "private-cookie")
        client._apiQuery.return_value = ObjDict({"code": -12, "message": "captcha required private-cookie request-secret private-account"})
        with patch.object(self.legacy, "logger") as logger:
            with self.assertRaises(self.legacy.CaptchaException):
                client.zhidaoQuery(URL + "?token=hidden", {"token": "request-secret"}, encrypt=False, setTimeStamp=False)
        client._apiQuery.assert_called_once()
        detail = json.loads(logger.warning.call_args.args[0].split("：", 1)[1])
        self.assertEqual(detail["path"], "/gateway/t/v1/learning/saveDatabaseIntervalTimeV2")
        self.assertEqual(detail["video_id"], "10")
        self.assertEqual(detail["played_time"], 12.5)
        for secret in ("private-cookie", "request-secret", "private-account", "?token"):
            self.assertNotIn(secret, json.dumps(detail))
        self.assertIsNone(probe.active)

    def test_manual_confirmation_replays_frozen_envelope_once_and_observes_three_reports(self):
        client, probe, clock, events, _ = self.prepare()
        form = {"secretStr": "original-payload", "dateFormate": 123000}
        captured = probe.capture(URL, form, "POST", "form")
        form["secretStr"] = "mutated-after-failure"
        def confirm():
            client._apiQuery.assert_not_called()
            atomic_json(probe.signal_file, {"task_id": probe.task_id, "probe_id": probe.active})
        clock.on_sleep = confirm
        self.assertEqual(probe.handle(captured).code, 0)
        client._apiQuery.assert_called_once_with(URL, data={"secretStr": "original-payload", "dateFormate": 123000}, method="POST", contentType="form")
        self.assertEqual(probe.observation["normal_reports"], 0)
        for _ in range(3):
            probe.success(URL)
        final = events[-1]
        self.assertEqual((final["normal_reports"], final["progress_delta_seconds"]), (3, 15))
        self.assertEqual(final["outcome"], "three_normal_reports")
        self.assertEqual(client.queryStudyInfo.call_count, 2)

    def test_real_query_bridge_can_recover_without_marking_captcha_as_fatal(self):
        import webui_worker as worker
        client, _, _, _, _ = self.prepare()
        client._apiQuery.side_effect = [ObjDict({"code": -12, "message": "captcha required"}), ObjDict({"code": 0, "data": {"ok": True}})]
        bridge = worker.LogBridge()
        worker.ISSUES.clear()
        self.legacy.logger.addHandler(bridge)
        try:
            with patch.object(worker, "OUTPUT", io.StringIO()):
                result = client.zhidaoQuery(URL, {"unchanged": "payload"}, encrypt=False, setTimeStamp=False)
            self.assertTrue(result.data.ok)
            self.assertEqual(client._apiQuery.call_count, 2)
            self.assertEqual(client._apiQuery.call_args_list[0], client._apiQuery.call_args_list[1])
            self.assertNotIn("verification", worker.ISSUES)
            self.assertNotIn("attention", worker.ISSUES)
        finally:
            self.legacy.logger.removeHandler(bridge)
            worker.ISSUES.clear()

    def test_old_or_wrong_signal_times_out_without_writes(self):
        client, probe, clock, events, _ = self.prepare()
        clock.on_sleep = lambda: atomic_json(probe.signal_file, {"task_id": "old", "probe_id": "old"})
        self.assertIsNone(probe.handle(probe.capture(URL, {}, "POST", "form")))
        client._apiQuery.assert_not_called()
        self.assertEqual(events[-1]["outcome"], "timeout")

    def test_worker_recovers_in_same_job_and_continues_without_false_verification_failure(self):
        import webui_worker as worker
        calls = []
        real_query = self.legacy.Fucker.zhidaoQuery
        class Client:
            zhidaoQuery = real_query
            uuid, cookies = "offline-account", []
            def __init__(self, **_):
                self.session = requests.Session()
                self.proxies = {}
                self.context = {"first": ObjDict({"course": {"recruitId": 7}, "videos": {10: {"id": 5, "lessonId": 6}}})}
                self.requests = 0
            def _apiQuery(self, *_args, **_kwargs):
                self.requests += 1
                return ObjDict({"code": -12 if self.requests == 1 else 0, "message": "captcha required"})
            def queryStudyInfo(self, *_):
                return ObjDict({"lv": {"5": {"studyTotalTime": 10 if self.requests < 3 else 25, "watchState": 0}}})
            def fuckZhidaoCourse(self, identifier):
                self.verification_video = {"course_id": identifier, "video_id": 10, "played_time": 20}
                for _ in range(4):
                    self.zhidaoQuery(URL, {"fixed": "original"}, encrypt=False, setTimeStamp=False)
                calls.append((identifier, self.requests))
            def fuckHikeCourse(self, identifier):
                calls.append((identifier, None))
        logger = logging.Logger("offline-probe-worker", logging.INFO)
        modules = {"utils": type("Utils", (), {"getRealPath": staticmethod(lambda value: value),
                   "cookie_jar_to_list": staticmethod(lambda value: value)}),
                   "logger": type("Logger", (), {"logger": logger}),
                   "fucker": type("Legacy", (), {"Fucker": Client})}
        with tempfile.TemporaryDirectory() as directory:
            signal = Path(directory) / "confirm.json"
            Path(directory, "cookies.json").write_text('[{"value":"private-cookie"}]')
            class Output(io.StringIO):
                def write(self, text):
                    result = super().write(text)
                    if text.startswith(worker.PREFIX):
                        event = json.loads(text[len(worker.PREFIX):])
                        if event.get("type") == "verification_probe" and event.get("phase") == "waiting":
                            atomic_json(signal, {"probe_id": event["probe_id"], "task_id": event["task_id"]})
                    return result
            output = Output()
            payload = {"action": "start", "root": directory, "local": directory, "runtime": directory,
                "task_id": "a" * 32, "settings": DEFAULTS.copy(), "ai": copy.deepcopy(AI_DEFAULTS),
                "verify_probe_enabled": True, "probe_signal_file": str(signal),
                "courses": [{"id": "first", "kind": "zhidao", "name": "first"},
                            {"id": "second", "kind": "hike", "name": "second"}]}
            worker.ISSUES.clear()
            with patch.dict(sys.modules, modules), patch.dict("os.environ", {"KILLZHS_VERIFY_PROBE": "1"}), \
                    patch.object(self.legacy, "logger", logger), patch.object(worker, "OUTPUT", output), \
                    patch.object(requests.sessions.Session, "request", side_effect=AssertionError("NETWORK_FORBIDDEN")):
                worker.run(payload)
            events = [json.loads(line[len(worker.PREFIX):]) for line in output.getvalue().splitlines()]
            self.assertEqual(calls, [("first", 5), ("second", None)])
            self.assertFalse(events[-1]["error"])
            self.assertNotIn("private-cookie", output.getvalue())
            observed = next(e for e in events if e.get("type") == "verification_probe" and e["phase"] == "observed")
            self.assertEqual(observed["normal_reports"], 3)
            self.assertEqual(observed["progress_delta_seconds"], 15)

    def test_stop_and_time_limit_interrupt_wait_without_progress_flush(self):
        for reason in ("stopped", "course_limit"):
            with self.subTest(reason=reason):
                client, probe, clock, _, _ = self.prepare()
                def stop():
                    if reason == "stopped":
                        atomic_json(client.task_control.stop_file, {"reason": reason})
                    else:
                        client.task_control.course_deadline = time.monotonic() - 1
                clock.on_sleep = stop
                flush = Mock()
                @finish_on_stop
                def operation(instance):
                    instance._stop_flush = flush
                    probe.handle(probe.capture(URL, {}, "POST", "form"))
                with self.assertRaises(TaskStopped) as result:
                    operation(client)
                self.assertEqual(result.exception.reason, reason)
                client._apiQuery.assert_not_called()
                flush.assert_not_called()

    def test_changed_cookie_rejects_comparison_without_rotating_tokens(self):
        client, probe, clock, events, _ = self.prepare()
        captured = probe.capture(URL, {}, "POST", "form")
        def confirm():
            client.session.cookies.set("changed", "new-cookie")
            atomic_json(probe.signal_file, {"task_id": probe.task_id, "probe_id": probe.active})
        clock.on_sleep = confirm
        client.prelearningNote = Mock(side_effect=AssertionError("token refresh forbidden"))
        self.assertIsNone(probe.handle(captured))
        client._apiQuery.assert_not_called()
        client.prelearningNote.assert_not_called()
        self.assertEqual(events[-1]["outcome"], "context_changed")

    def test_rejection_is_not_repeated_or_promoted_to_another_level(self):
        client, probe, _, events, _ = self.prepare(code=-12)
        self.assertIsNone(probe.handle(probe.capture(URL, {}, "POST", "form")))
        client._apiQuery.assert_called_once()
        self.assertEqual(events[-1]["outcome"], "original_request_rejected")

    def test_cleanup_and_nonshared_endpoints_never_wait(self):
        client, probe, _, _, _ = self.prepare()
        self.assertIsNone(probe.capture("https://example.test/other", {}, "POST", "form"))
        with client.task_control.cleanup():
            self.assertIsNone(probe.capture(URL, {}, "POST", "form"))
        probe.course_kind = "ai"
        self.assertIsNone(probe.capture(URL, {}, "POST", "form"))

    def test_popup_captcha_propagates_without_an_additional_progress_report(self):
        client, _, _, _, _ = self.prepare(enabled=False)
        client.speed, client.end_thre, client.progressbar_view = 2, 1, False
        client._checkCookies = lambda: None
        client._checkTimeLimit = lambda _: None
        client._sessionReady = lambda *_: None
        client.watchVideo = lambda *_: None
        client.getZhidaoContext = lambda _: ObjDict({"fucked_time": 0,
            "videos": {10: {"studyTotalTime": 0, "watchState": 0, "name": "video", "videoSec": 2, "videoId": 10}}})
        client.prelearningNote = lambda *_: ObjDict({"studiedLessonDto": {"id": 1}})
        client.loadVideoPointerInfo = lambda *_: ObjDict({"questionPoint": [{"timeSec": 1, "questionIds": [1]}]})
        client.threeDimensionalCourseWare = lambda *_: None
        client.lessonPopoupExam = Mock(side_effect=self.legacy.CaptchaException("captcha required"))
        client.saveDatabaseIntervalTimeV2 = Mock()
        with patch.object(self.legacy.time, "sleep", lambda _: None), patch.object(self.legacy, "random", return_value=1):
            with self.assertRaises(self.legacy.CaptchaException):
                client.fuckZhidaoVideo("course", 10)
        client.saveDatabaseIntervalTimeV2.assert_not_called()

    def test_replay_has_one_wire_attempt_and_does_not_follow_redirects(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from requests.adapters import Retry
        for status in (500, 307):
            with self.subTest(status=status):
                hits = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_): pass
                    def do_POST(self):
                        hits.append(self.path)
                        self.rfile.read(int(self.headers.get("Content-Length", 0)))
                        body = b'{"code":-12}' if len(hits) == 1 else b'{"code":0}'
                        self.send_response(status if len(hits) == 1 else 200)
                        if status == 307:
                            self.send_header("Location", "/unexpected-replay")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client, probe, _, _, _ = self.prepare()
                    client.headers = dict(client.session.headers)
                    client.session.trust_env = False
                    local_url = f"http://127.0.0.1:{server.server_port}/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"
                    adapter = client.session.get_adapter(local_url)
                    retries = Retry(total=2, status_forcelist=[500], allowed_methods={"POST"})
                    adapter.max_retries = retries
                    client._apiQuery = self.legacy.Fucker._apiQuery.__get__(client)
                    self.assertIsNone(probe.handle(probe.capture(local_url, {"test": "body"}, "POST", "form")))
                    self.assertEqual(len(hits), 1)
                    self.assertIs(adapter.max_retries, retries)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(2)

    def test_confirmation_is_bound_to_task_and_probe_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, demo=True)
            try:
                controller.status, controller.action = "running", "start"
                controller.task_id = "d" * 32
                controller.verify_probe_enabled = True
                controller.probe_signal_file = Path(directory) / "signal.json"
                controller.current = course_key(controller.courses[0])
                controller.job_courses = [controller.courses[0]]
                event = {"type": "verification_probe", "phase": "waiting", "task_id": controller.task_id, "probe_id": "e" * 32}
                controller.event(event)
                controller.event({**event, "phase": "baseline", "baseline": None})
                self.assertTrue(controller.state()["verification_probe"]["awaiting_confirmation"])
                with self.assertRaises(InputError):
                    controller.dispatch({"action": "confirm_verification", "task_id": "old", "probe_id": "e" * 32})
                request = {"action": "confirm_verification", "task_id": controller.task_id, "probe_id": "e" * 32}
                controller.dispatch(request)
                signal = json.loads(controller.probe_signal_file.read_text())
                self.assertEqual(signal["task_id"], controller.task_id)
                with self.assertRaises(InputError):
                    controller.dispatch(request)
                self.assertTrue(controller.state()["verification_probe"]["confirmation_sent"])
            finally:
                controller.status = "idle"
                controller.task_id = None
                controller.close()


if __name__ == "__main__":
    unittest.main()
