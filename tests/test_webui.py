"""Offline integration checks; never connect to a real learning account."""

from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webui import Controller, DEFAULTS, Handler, InputError, PREFIX, ThreadingHTTPServer, validate_settings
import webui_worker as worker


class UITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = Controller(self.root)

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_validation_rejects_invalid_and_nonfinite_values(self):
        for settings in ({"speed": float("nan")}, {"speed": True}, {"limit": -1},
                         {"limit": 0.5}, {"remember": "yes"}, {"threshold": 0},
                         {"command": "arbitrary"}, [], None):
            with self.subTest(settings=settings), self.assertRaises(InputError):
                validate_settings(settings)
        self.assertEqual(validate_settings({}), DEFAULTS)

    def test_settings_and_courses_preserve_legacy_files(self):
        originals = {"config.json": '{"password":"original-private"}',
                     "cookies.json": '[{"value":"cookie-private"}]',
                     "execution.json": '[{"name":"Original","id":"123"}]'}
        for name, text in originals.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.controller.dispatch({"action": "settings", "settings": {"speed": 2}})
        self.controller.dispatch({"action": "add", "course": {"id": "123", "name": "测试"}})
        for name, text in originals.items():
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), text)
        state = self.controller.state()
        self.assertNotIn("original-private", json.dumps(state))
        self.assertNotIn("cookie-private", json.dumps(state))
        remembered = self.controller.local / "cookies.json"
        remembered.write_text("[]", encoding="utf-8")
        self.controller.dispatch({"action": "settings", "settings": {"remember": False}})
        self.assertFalse(remembered.exists())
        self.assertTrue((self.root / "cookies.json").exists())

    def test_partial_sync_preserves_other_course_category(self):
        self.controller.dispatch({"action": "add", "course": {"id": "123", "name": "校园课", "kind": "hike"}})
        self.controller.event({"type": "courses", "courses": [{"id": "abc", "name": "共享课", "kind": "zhidao"}], "kinds": ["zhidao"]})
        self.assertEqual({c["kind"] for c in self.controller.courses}, {"hike", "zhidao"})

    def test_start_requires_login_and_known_selection(self):
        with self.assertRaises(InputError):
            self.controller.dispatch({"action": "start", "courses": ["hike:123"]})
        self.controller.logged_in = True
        for selection in ([], ["hike:unknown"], [None], "hike:123"):
            with self.subTest(selection=selection), self.assertRaises(InputError):
                self.controller.dispatch({"action": "start", "courses": selection})

    def fake_worker(self, source):
        script = self.root / "fake_worker.py"
        script.write_text(source, encoding="utf-8")
        self.controller.worker_command = [sys.executable, "-u", str(script)]
        self.controller.dependencies = []

    def test_worker_completion_is_structured_and_raw_output_hidden(self):
        self.fake_worker("import sys,json\npayload=json.loads(sys.stdin.readline())\nprint('RAW-SECRET-TRACEBACK')\n"
                         f"print({PREFIX!r}+json.dumps({{'type':'log','message':'checked','level':'info'}}))\n"
                         f"print({PREFIX!r}+json.dumps({{'type':'result','message':'complete','error':False}}))\n")
        self.controller.dispatch({"action": "refresh"})
        self.controller.thread.join(timeout=5)
        state = self.controller.state()
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["message"], "complete")
        self.assertIsNotNone(state["ended_at"])
        self.assertNotIn("RAW-SECRET", json.dumps(state))
        self.assertTrue(any(row["message"] == "checked" for row in state["logs"]))

    def test_stop_terminates_worker_and_allows_restart(self):
        self.fake_worker("import sys,time\nsys.stdin.readline()\ntime.sleep(60)\n")
        self.controller.dispatch({"action": "refresh"})
        deadline = time.monotonic() + 3
        while self.controller.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        process = self.controller.process
        self.assertIsNotNone(process)
        with self.assertRaises(InputError):
            self.controller.dispatch({"action": "refresh"})
        self.controller.stop()
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.controller.thread.is_alive())
        self.assertEqual(self.controller.status, "stopped")
        self.fake_worker("import sys\nsys.stdin.readline()\n")
        self.controller.dispatch({"action": "refresh"})
        self.controller.thread.join(timeout=5)
        self.assertEqual(self.controller.status, "done")

    def test_http_protects_local_actions_and_private_files(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.controller = self.controller
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request(method, path, body=body, headers=headers or {})
            result = connection.getresponse()
            status, content = result.status, result.read()
            connection.close()
            return status, content

        try:
            self.assertEqual(request("GET", "/")[0], 200)
            self.assertEqual(request("GET", "/api/state")[0], 200)
            for path in ("/config.json", "/cookies.json", "/.local/webui/cookies.json", "/../config.json"):
                self.assertEqual(request("GET", path)[0], 404)
            body = json.dumps({"action": "settings", "settings": {"limit": 10}})
            self.assertEqual(request("POST", "/api/action", body)[0], 403)
            headers = {"X-Local-Token": self.controller.token, "Content-Type": "application/json"}
            self.assertEqual(request("POST", "/api/action", body, {**headers, "Origin": "http://external.invalid"})[0], 403)
            self.assertEqual(request("GET", "/api/state", headers={"Host": "external.invalid"})[0], 403)
            self.assertEqual(request("POST", "/api/action", body, headers)[0], 200)
            self.assertEqual(self.controller.settings["limit"], 10)
            self.assertEqual(request("POST", "/api/action", "{broken", headers)[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_broken_saved_settings_fall_back_to_defaults(self):
        self.controller.local.mkdir(parents=True, exist_ok=True)
        path = self.controller.local / "settings.json"
        for content in ('[]', '{"speed":false}', '{"limit":-1}'):
            path.write_text(content, encoding="utf-8")
            controller = Controller(self.root)
            try:
                self.assertEqual(controller.settings, DEFAULTS)
            finally:
                controller.close()


class WorkerTest(unittest.TestCase):
    def setUp(self):
        worker.SECRETS.clear()
        worker.ISSUES.clear()

    def test_secret_redaction(self):
        worker.remember_secrets({"password": "private-password", "api_key": "private-key",
                                 "cookies": [{"value": "private-cookie"}]})
        result = worker.redact("private-password private-key private-cookie https://example.test/path?pwd=one-time token=abc")
        for secret in ("private-password", "private-key", "private-cookie", "one-time", "abc"):
            self.assertNotIn(secret, result)

    def test_installed_client_imports_without_network_or_legacy_writes(self):
        # Real dependencies and legacy modules, but no cookies and no platform call.
        # Patch the socket connection in the child to make the no-network limit explicit.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"root": str(root), "local": str(root / "local"), "runtime": str(root / "runtime"),
                       "settings": {**DEFAULTS, "remember": False}, "action": "refresh", "courses": []}
            (root / "runtime").mkdir()
            project = str(Path(__file__).resolve().parents[1])
            source = ("import sys,socket; sys.path.insert(0,sys.argv[1]); "
                      "socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('NETWORK_FORBIDDEN')); "
                      "import webui_worker; raise SystemExit(webui_worker.main())")
            result = subprocess.run([sys.executable, "-c", source, project],
                input=json.dumps(payload) + "\n", capture_output=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 1)
            events = [json.loads(line[len(PREFIX):]) for line in result.stdout.splitlines() if line.startswith(PREFIX)]
            self.assertTrue(any("没有可用的登录状态" in e.get("message", "") for e in events), result.stdout + result.stderr)
            self.assertNotIn("NETWORK_FORBIDDEN", result.stdout)
            self.assertTrue((root / "runtime" / "logs").is_dir())

    def test_real_worker_bridge_with_fake_platform(self):
        # Exercise the actual worker import path, cookie handoff and course dispatch
        # with an in-memory platform client; no requests are sent.
        calls = []

        class Client:
            uuid = "fake-user-id"
            cookies = []

            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def login(self, qr_callback, **kwargs):
                qr_callback(b"fake-png")
                self.cookies = [{"value": "private-cookie"}]

            def getZhidaoList(self):
                return [{"secret": "shared1", "courseName": "示例共享课"}]

            def getHikeList(self):
                raise RuntimeError("offline campus")

            def fuckZhidaoCourse(self, identifier):
                calls.append(("run", identifier))

            def fuckHikeCourse(self, identifier):
                calls.append(("run-campus", identifier))

            def fuckCourse(self, identifier):
                calls.append(("auto", identifier))

        utils = types.SimpleNamespace(getRealPath=lambda path: path, cookie_jar_to_list=lambda cookies: cookies)
        logger = types.SimpleNamespace(setLevel=lambda _: None, addHandler=lambda _: None)
        modules = {"utils": utils, "logger": types.SimpleNamespace(logger=logger), "fucker": types.SimpleNamespace(Fucker=Client)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"root": str(root), "local": str(root / "local"), "runtime": str(root / "runtime"),
                       "settings": DEFAULTS.copy(), "action": "login", "courses": []}
            output = io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(worker, "OUTPUT", output):
                worker.run(payload)
                events = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
                self.assertTrue(any(e["type"] == "qr" for e in events))
                self.assertTrue(any(e["type"] == "courses" and e["kinds"] == ["zhidao"] for e in events))
                self.assertTrue(events[-1]["error"])
                self.assertTrue((root / "local" / "cookies.json").exists())
                self.assertFalse((root / "cookies.json").exists())
                payload.update(action="start", courses=[{"id": "shared1", "kind": "zhidao", "name": "示例共享课"}])
                output.seek(0)
                output.truncate()
                worker.run(payload)
                self.assertIn(("run", "shared1"), calls)
                self.assertIn('"status": "done"', output.getvalue())
                self.assertNotIn("private-cookie", output.getvalue())


if __name__ == "__main__":
    unittest.main()
