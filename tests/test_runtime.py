import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch, Mock

from killzhs_config import AI_DEFAULTS
from killzhs_runtime import TaskControl, TaskStopped
from webui import Controller, DEFAULTS, PREFIX, normalize_course
import webui_worker as worker
from ObjDict import ObjDict


class RuntimeTest(unittest.TestCase):
    def test_worker_main_reports_deadline_stop_as_a_normal_limited_result(self):
        output = io.StringIO()
        worker.ISSUES.clear()
        with patch.object(sys, "stdin", io.StringIO("{}")), patch.object(worker, "OUTPUT", output), \
                patch.object(worker, "run", side_effect=TaskStopped("limited")):
            self.assertEqual(worker.main(), 0)
        events = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["status"], "limited")
        self.assertFalse(events[-1]["error"])

    def test_monotonic_deadline_cleanup_and_course_reset(self):
        with patch("killzhs_runtime.time.time", return_value=100), \
                patch("killzhs_runtime.time.monotonic", return_value=10) as clock:
            control = TaskControl(deadline=105)
            clock.return_value = 14
            control.check()
            clock.return_value = 15
            with self.assertRaises(TaskStopped) as stopped:
                control.check()
            self.assertEqual(stopped.exception.reason, "limited")
            with control.cleanup():
                control.check()
            with self.assertRaises(TaskStopped):
                control.check()
            course = TaskControl()
            course.begin_course(1)
            clock.return_value = 76
            with self.assertRaises(TaskStopped) as stopped:
                course.check()
            self.assertEqual(stopped.exception.reason, "course_limit")
            course.begin_course(1)
            course.check()

    def test_stop_request_is_nonblocking_and_child_can_finish_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "cooperative.py"
            script.write_text(
                "import sys,json,time\nfrom pathlib import Path\n"
                "p=json.loads(sys.stdin.readline())\n"
                "while not Path(p['stop_file']).is_file(): time.sleep(.01)\n"
                "time.sleep(.2)\n"
                "print(" + repr(PREFIX) + "+json.dumps({'type':'result','message':'cleanup finished','status':'limited'}),flush=True)\n",
                encoding="utf-8")
            controller = Controller(root, worker_command=[sys.executable, "-u", str(script)])
            try:
                controller.logged_in = True
                controller.courses = [normalize_course({"id": "12", "kind": "hike"})]
                controller.dispatch({"action": "start", "courses": ["hike:12"]})
                started = time.monotonic()
                controller.expire(controller.job_id)
                self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(controller.state()["stop_requested"], "limited")
                controller.thread.join(5)
                self.assertEqual(controller.status, "limited")
                self.assertIn("cleanup finished", controller.task_log(controller.history[-1]["id"])["text"])
                self.assertIsNone(controller.stop_requested)
            finally:
                controller.close()

    def test_unresponsive_worker_is_forced_after_grace_and_stale_timer_is_safe(self):
        with tempfile.TemporaryDirectory() as directory, patch("webui.STOP_GRACE_SECONDS", .05):
            root = Path(directory)
            script = root / "stuck.py"
            script.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(60)\n")
            controller = Controller(root, worker_command=[sys.executable, "-u", str(script)])
            try:
                controller.logged_in = True
                controller.courses = [normalize_course({"id": "12", "kind": "hike"})]
                controller.dispatch({"action": "start", "courses": ["hike:12"]})
                first = controller.job_id
                controller.expire(first)
                controller.thread.join(5)
                self.assertFalse(controller.thread.is_alive())
                self.assertEqual(controller.status, "limited")
                controller.dispatch({"action": "start", "courses": ["hike:12"]})
                controller.stop(expected_job=first, force=True)
                self.assertEqual(controller.status, "running")
            finally:
                controller.close()

    def test_per_course_limit_moves_to_next_course_and_cleanup_unwinds(self):
        calls = []
        class Client:
            uuid = "test-uuid"
            cookies = []
            def __init__(self, **_): pass
            def fuckHikeCourse(self, identifier):
                calls.append(identifier)
                if identifier == "first":
                    try:
                        raise TaskStopped("course_limit")
                    finally:
                        calls.append("cleanup")
        modules = {"utils": types.SimpleNamespace(getRealPath=lambda value: value, cookie_jar_to_list=lambda value: value),
                   "logger": types.SimpleNamespace(logger=types.SimpleNamespace(setLevel=lambda _: None, addHandler=lambda _: None)),
                   "fucker": types.SimpleNamespace(Fucker=Client)}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "cookies.json").write_text('[{"value":"fake-cookie"}]')
            payload = {"root": directory, "local": directory, "runtime": directory,
                       "settings": DEFAULTS.copy(), "ai": copy.deepcopy(AI_DEFAULTS), "action": "start",
                       "courses": [{"id": name, "kind": "hike", "name": name} for name in ("first", "second")]}
            output = io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(worker, "OUTPUT", output):
                worker.run(payload)
            self.assertEqual(calls, ["first", "cleanup", "second"])
            events = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
            self.assertEqual([event["status"] for event in events if event["type"] == "course"],
                             ["running", "limited", "running", "done"])

    def test_stop_file_write_failure_falls_back_to_forced_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "stuck.py"
            script.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(60)\n")
            controller = Controller(root, worker_command=[sys.executable, "-u", str(script)])
            try:
                controller.logged_in = True
                controller.courses = [normalize_course({"id": "12", "kind": "hike"})]
                controller.dispatch({"action": "start", "courses": ["hike:12"]})
                with patch("webui.atomic_json", side_effect=OSError("disk full")):
                    controller.stop()
                self.assertEqual(controller.status, "stopped")
                self.assertFalse(controller.thread.is_alive())
            finally:
                controller.close()


class VideoLoopsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the real legacy implementation while keeping its logger files local.
        import utils
        real_path = utils.getRealPath
        log_dir = Path(__file__).resolve().parents[1] / ".local" / "unit-logs"
        with patch.object(utils, "getRealPath", side_effect=lambda value: str(log_dir) if str(value).strip("./") == "logs" else real_path(value)):
            import fucker
        cls.legacy = fucker

    def client(self):
        client = object.__new__(self.legacy.Fucker)
        client.task_control = TaskControl()
        client.speed, client.end_thre, client.progressbar_view = 2, 1, False
        client._checkCookies = lambda: None
        client._checkTimeLimit = lambda _: None
        client._sessionReady = lambda *args: None
        client.watchVideo = lambda _: None
        return client

    def test_shared_video_flushes_only_unsent_progress_on_stop(self):
        client = self.client()
        ctx = ObjDict({"fucked_time": 0, "videos": {1: {"studyTotalTime": 0, "watchState": 0, "name": "video", "videoSec": 100, "videoId": 1}}})
        client.getZhidaoContext = lambda _: ctx
        client.prelearningNote = lambda *args: ObjDict({"studiedLessonDto": {"id": 1}})
        client.loadVideoPointerInfo = lambda *args: ObjDict({"questionPoint": []})
        client.threeDimensionalCourseWare = lambda _: None
        submitted = []
        client.saveDatabaseIntervalTimeV2 = lambda *args: (client.task_control.check(), submitted.append(args))
        ticks = [0]
        def sleep(_):
            ticks[0] += 1
            if ticks[0] == 2:
                client.task_control.deadline = time.monotonic() - 1
                raise TaskStopped("limited")
        with patch.object(self.legacy, "time", types.SimpleNamespace(sleep=sleep)), \
                patch.object(self.legacy, "random", return_value=1), \
                patch.object(self.legacy, "progressBar", lambda *args, **kwargs: None):
            with self.assertRaises(TaskStopped):
                client.fuckZhidaoVideo("course", 1)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][2:4], (2, 0))

    def test_hike_video_exits_when_server_returns_exact_endpoint(self):
        client = self.client()
        client.getHikeContext = lambda _: ObjDict({"fucked_time": 0})
        client.stuViewFile = lambda *args: ObjDict({"dataId": 1, "totalTime": 2})
        client.saveStuStudyRecord = Mock(side_effect=[2, AssertionError("Repeated endpoint submission")])
        with patch.object(self.legacy, "time", types.SimpleNamespace(sleep=lambda _: None, time=lambda: 100)), \
                patch.object(self.legacy, "progressBar", lambda *args, **kwargs: None):
            client.fuckHikeVideo("course", "video")
        client.saveStuStudyRecord.assert_called_once()

    def test_ai_video_checks_cancellation_between_reports(self):
        client = self.client()
        client.zhidaoQuery = lambda *args, **kwargs: ObjDict({"data": [{"time": 100}]})
        client.reportAiVideoProcess = Mock()
        def sleep(_):
            raise TaskStopped("limited")
        with patch.object(self.legacy, "time", types.SimpleNamespace(sleep=sleep)):
            with self.assertRaises(TaskStopped):
                client.fuckAiVideo(1, 2, 3, 4)
        client.reportAiVideoProcess.assert_called_once()

    def test_ppt_remote_delete_runs_when_extraction_unwinds_on_stop(self):
        converter = object.__new__(self.legacy.PptToTxt)
        converter._PptToTxt__delete_immediately = True
        converter._PptToTxt__file_cache = {}
        converter._PptToTxt__client = Mock()
        converter._PptToTxt__client.files.content.side_effect = TaskStopped("limited")
        with self.assertRaises(TaskStopped):
            converter._PptToTxt__extractText("test-upload")
        converter._PptToTxt__client.files.delete.assert_called_once_with(file_id="test-upload")


if __name__ == "__main__":
    unittest.main()
