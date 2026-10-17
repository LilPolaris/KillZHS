"""Actual jobs against the offline platform and spawn-process lifecycle tests."""

import copy
import json
import random
import threading
import time

import pytest
import requests

from killzhs.models import AIConfig, RunSettings
from killzhs.worker.jobs import execute
from killzhs.worker.process import WorkerProcess

from .protocol import fake_platform as fake
from .protocol.scenarios import AI_BASE, COOKIES


def _noisy_entry(*args):
    """Runs inside spawn, injecting noisy dependencies into the real entry wrapper."""
    import sys

    from killzhs.worker import jobs
    from killzhs.worker.process import _entry

    def noisy_job(_payload, _directory, *, emit, **_):
        print("RAW-STDOUT-SECRET")
        print("RAW-STDERR-SECRET", file=sys.stderr)
        emit("log", message="structured checked", level="info")
        emit("result", message="structured complete", error=False)
        return 0

    jobs.execute = noisy_job
    _entry(*args)


@pytest.fixture
def offline(monkeypatch):
    clock, transport = fake.Clock(), fake.Transport()
    monkeypatch.setattr(requests.sessions.Session, "request", transport.original)
    for key in ("time", "monotonic", "sleep"):
        monkeypatch.setattr(time, key, getattr(time, key))
    transport.install(clock)
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    platform = transport.platform = fake.Platform()
    state = random.getstate()
    random.seed(0)
    yield clock, transport, platform
    random.setstate(state)


def payload(action, **values):
    return {"action": action, "settings": RunSettings().model_dump(), "ai": AIConfig().model_dump(),
            "cookies": copy.deepcopy(COOKIES), "courses": [], **values}


def run_job(job, directory, **kwargs):
    events = []
    code = execute(job, directory, emit=lambda kind, **values: events.append(copy.deepcopy({"type": kind, **values})), **kwargs)
    return code, events


def test_login_and_sync_use_explicit_clients_without_writing_credentials(offline, tmp_path):
    code, events = run_job(payload("login", cookies=[]), tmp_path)
    assert code == 0
    assert any(event["type"] == "qr" for event in events)
    assert any(event["type"] == "cookies" and event["cookies"] for event in events)
    assert any(event["type"] == "auth" and event["logged_in"] for event in events)
    sync = next(event for event in events if event["type"] == "courses")
    assert set(sync["kinds"]) == {"zhidao", "hike", "ai"}
    assert not events[-1]["error"]
    assert not list(tmp_path.glob("*.json"))


def test_incomplete_sync_keeps_good_rows_and_marks_affected_category(offline, tmp_path, monkeypatch):
    from killzhs.platform.hike import HikeCourses
    from killzhs.platform.shared import SharedCourses
    monkeypatch.setattr(SharedCourses, "list", lambda _: [{"secret": "valid", "courseName": "保留"}, {}])
    monkeypatch.setattr(HikeCourses, "list", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    _, events = run_job(payload("refresh"), tmp_path)
    sync = next(event for event in events if event["type"] == "courses")
    assert sync["incomplete_kinds"] == ["zhidao"]
    assert "hike" not in sync["kinds"]
    assert sync["courses"][0]["id"] == "valid"
    assert events[-1]["error"]


def test_course_stop_limit_advances_and_errors_mark_attention(offline, tmp_path, monkeypatch):
    from killzhs.platform.shared import SharedCourses
    from killzhs.runtime import TaskStopped
    calls = []

    def course(_, identifier):
        calls.append(identifier)
        if identifier == "first":
            raise TaskStopped("course_limit")
        import logging
        logging.getLogger("killzhs.platform").error("an error that must require attention")

    monkeypatch.setattr(SharedCourses, "run", course)
    courses = [{"id": key, "name": key, "kind": "zhidao"} for key in ("first", "second")]
    _, events = run_job(payload("start", courses=courses), tmp_path)
    assert calls == ["first", "second"]
    assert [(e["key"], e["status"]) for e in events if e["type"] == "course"] == [
        ("zhidao:first", "running"), ("zhidao:first", "limited"),
        ("zhidao:second", "running"), ("zhidao:second", "attention"),
    ]
    assert events[-1]["error"]


def test_per_course_limit_finishes_cleanup_before_next_course(offline, tmp_path, monkeypatch):
    from killzhs.platform.hike import HikeCourses
    from killzhs.runtime import TaskStopped

    calls = []

    def course(client, identifier):
        calls.append(identifier)
        assert not client.control.cleaning
        if identifier == "first":
            client.control.course_deadline = time.monotonic() - 1
            try:
                client.control.check()
            except TaskStopped:
                with client.control.cleanup():
                    calls.append("cleanup")
                    client.control.check()
                raise
        client.control.check()  # The previous course's expired deadline was reset.

    monkeypatch.setattr(HikeCourses, "run", course)
    courses = [{"id": name, "name": name, "kind": "hike"} for name in ("first", "second")]
    code, events = run_job(payload("start", courses=courses), tmp_path)
    assert code == 0 and not events[-1]["error"]
    assert calls == ["first", "cleanup", "second"]
    assert [event["status"] for event in events if event["type"] == "course"] == ["running", "limited", "running", "done"]


def test_captcha_stops_batch_and_does_not_start_next_course(offline, tmp_path):
    offline[1].platform = fake.Platform(captcha_on_report=True)
    courses = [{"id": fake.SHARED_ID, "name": name, "kind": "zhidao"} for name in ("first", "second")]
    _, events = run_job(payload("start", courses=courses), tmp_path)
    assert len([event for event in events if event["type"] == "course" and event["status"] == "running"]) == 1
    assert events[-1]["reason"] == "verification"


def test_worker_recovers_in_same_job_and_continues_without_false_verification_failure(offline, tmp_path, monkeypatch):
    from killzhs.platform.shared import SharedCourses

    monkeypatch.setenv("KILLZHS_VERIFY_PROBE", "1")
    platform = offline[1].platform = fake.Platform(captcha_on_report=True)
    events, confirmation = [], [None]

    def run_shared(client, course_id):
        context = client.context(course_id)
        client.http.ready(context["cookies"], context["headers"])
        client.http.video = {"course_id": course_id, "video_id": 5004, "played_time": 20}
        for _ in range(4):
            client.http.zhidao("https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTimeV2",
                               {"fixed": "original"}, encrypt=False, timestamp=False)

    monkeypatch.setattr(SharedCourses, "run", run_shared)

    def receive(kind, **values):
        events.append(copy.deepcopy({"type": kind, **values}))
        if kind == "verification_probe" and values["phase"] == "waiting":
            confirmation[0] = values["probe_id"]
            platform.captcha_on_report = False

    courses = [{"id": fake.SHARED_ID, "name": "first", "kind": "zhidao"},
               {"id": fake.HIKE_ID, "name": "second", "kind": "hike"}]
    code = execute(payload("start", courses=courses, verify_probe_enabled=True, task_id="a" * 32), tmp_path,
                   emit=receive, confirmed=lambda: confirmation[0])
    assert code == 0 and not events[-1]["error"]
    completed = [event for event in events if event["type"] == "course" and event["status"] == "done"]
    assert [event["key"] for event in completed] == [f"zhidao:{fake.SHARED_ID}", f"hike:{fake.HIKE_ID}"]
    observed = next(event for event in events if event["type"] == "verification_probe" and event["phase"] == "observed")
    assert observed["normal_reports"] == 3
    assert "verification" not in (events[-1].get("reason"),)


def test_assessment_job_scans_and_completes_with_confirmed_results(offline, tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(Path(__file__).parents[1] / ".local" / "tokenizer"))
    course = {"id": fake.SHARED_ID, "kind": "zhidao", "name": "示例共享课"}
    _, scan = run_job(payload("assessment_scan", courses=[course]), tmp_path)
    items = next(event["items"] for event in scan if event["type"] == "assessments")
    items = [row for row in items if row["state"] in (1, 2, 5)]
    _, events = run_job(payload("assessment_run", ai=copy.deepcopy(AI_BASE), courses=[course], assessments=items), tmp_path)
    assert not events[-1]["error"], events
    assert any(event["type"] == "assessment_result" and event["state"] == "submitted" for event in events)
    assert any(event["type"] == "ai_usage" for event in events)


def test_stops_and_errors_are_structured_and_redacted(offline, tmp_path, monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *_: pytest.fail("unexpected network"))
    _, events = run_job(payload("refresh", cookies=[]), tmp_path)
    assert events[-1]["error"]
    _, events = run_job(payload("refresh"), tmp_path, stop_signal=lambda: "limited")
    assert events[-1]["status"] == "limited"
    assert not events[-1]["error"]
    from killzhs.platform.shared import SharedCourses
    monkeypatch.setattr(SharedCourses, "list", lambda _: (_ for _ in ()).throw(RuntimeError("saved-secret")))
    job = payload("refresh", ai={**AIConfig().model_dump(), "openai": {"api_key": "saved-secret"}})
    _, events = run_job(job, tmp_path)
    public = [event for event in events if event["type"] != "cookies"]
    assert "saved-secret" not in json.dumps(public)


def test_spawned_diagnose_drains_events_before_exit(tmp_path):
    events, exits, done = [], [], threading.Event()

    def exited(code):
        exits.append(code)
        assert events[-1]["type"] == "result"
        done.set()

    process = WorkerProcess(payload("diagnose"), tmp_path, events.append, exited)
    process.start()
    try:
        assert done.wait(20)
        process.join(2)
        assert not process.is_alive()
        assert exits == [0]
        assert not events[-1]["error"]
    finally:
        process.terminate()
        process.join(5)


@pytest.mark.parametrize("missing", ["openai", "tiktoken"])
def test_diagnose_checks_dependencies_that_are_otherwise_lazy(tmp_path, monkeypatch, missing):
    import importlib
    original = importlib.import_module

    def load(name, *args, **kwargs):
        if name == missing:
            raise ModuleNotFoundError(f"No module named '{missing}'")
        return original(name, *args, **kwargs)

    monkeypatch.setattr("killzhs.worker.jobs.importlib.import_module", load)
    code, events = run_job(payload("diagnose"), tmp_path)
    assert code == 1 and events[-1]["error"]
    assert not any("检查通过" in event.get("message", "") for event in events)


def test_spawned_job_sees_stop_signal_before_requests(tmp_path):
    events, done = [], threading.Event()
    process = WorkerProcess(payload("refresh"), tmp_path, events.append, lambda _: done.set())
    process.request_stop("limited")
    process.start()
    try:
        assert done.wait(20)
        assert events[-1]["status"] == "limited"
        assert not events[-1]["error"]
    finally:
        process.terminate()
        process.join(5)


def test_actual_spawn_hides_raw_stdout_and_stderr_from_page_and_task_log(tmp_path, monkeypatch):
    from killzhs.app.controller import Controller

    monkeypatch.setattr("killzhs.worker.process._entry", _noisy_entry)
    controller = Controller(data_dir=tmp_path)
    try:
        controller.action_request("diagnose", {})
        thread = controller.thread
        thread.join(timeout=20)
        assert not thread.is_alive()
        state = controller.state()
        assert state["status"] == "done"
        assert state["message"] == "structured complete"
        assert any(row["message"] == "structured checked" for row in state["logs"])
        log = controller.task_log(state["history"][0]["id"])
        assert "structured checked" in log["text"]
        for secret in ("RAW-STDOUT-SECRET", "RAW-STDERR-SECRET"):
            assert secret not in json.dumps(state)
            assert secret not in log["text"]
    finally:
        controller.close()
