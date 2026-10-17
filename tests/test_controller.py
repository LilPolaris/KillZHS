"""Controller behavior from the old workbench, exercised without platform calls."""

import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from killzhs.app.actions import course_key, normalize_course
from killzhs.app.controller import Controller
from killzhs.app.store import atomic_json
from killzhs.models import InputError


class FakeWorker:
    """Controllable WorkerProcess stand-in. Callbacks follow the real API."""

    instances = []

    def __init__(self, payload, data_dir, on_event, on_exit):
        self.payload = copy.deepcopy(payload)
        self.data_dir = data_dir
        self.on_event, self.on_exit = on_event, on_exit
        self.alive = False
        self.exitcode = None
        self.pid = 123
        self.thread = None
        self.request_stop = Mock()
        self.confirm_verification = Mock()
        self.terminate = Mock(side_effect=lambda: self.finish(-15))
        self.kill = Mock(side_effect=lambda: self.finish(-9))
        self.join = Mock()
        self.instances.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def emit(self, event):
        self.on_event(event)

    def finish(self, code=0, *, result=None):
        if result is not None:
            self.emit({"type": "result", "message": result, "error": False})
        self.alive, self.exitcode = False, code
        self.on_exit(code)


@pytest.fixture
def controller(tmp_path):
    instance = Controller(data_dir=tmp_path, worker_factory=FakeWorker)
    instance.dependencies = []
    instance.courses = [normalize_course({"id": "first", "kind": "zhidao", "name": "第一门课"}),
                        normalize_course({"id": "second", "kind": "zhidao", "name": "第二门课"}),
                        normalize_course({"id": "12", "kind": "hike", "name": "校内课"})]
    instance.logged_in = True
    try:
        yield instance
    finally:
        instance.close()


def start(controller, keys=None):
    controller.dispatch({"action": "start", "courses": keys or ["zhidao:first"]})
    return controller.process


def test_legacy_state_contract_and_detached_snapshots(controller):
    expected = {"token", "demo", "status", "action", "logged_in", "qr", "progress", "current", "message",
                "started_at", "ended_at", "settings", "courses", "logs", "missing_dependencies", "log_cursor",
                "logs_reset", "defaults", "saved_session", "version", "ai", "plan", "next_run", "history_revision",
                "deadline", "ai_requests", "active_ai_max_requests", "stop_requested", "verification",
                "verification_probe", "verify_probe_enabled", "verify_probe_available", "notifications",
                "assessments", "assessment_results", "app", "history"}
    snapshot = controller.state()
    assert set(snapshot) == expected
    snapshot["courses"].clear()
    snapshot["settings"]["limit"] = 99
    assert len(controller.courses) == 3
    assert controller.settings["limit"] == 0


def test_incremental_logs_history_revision_and_reconnect(controller):
    first = controller.state()
    controller.log("new log")
    delta = controller.state(first["log_cursor"], first["history_revision"])
    assert [row["message"] for row in delta["logs"]] == ["new log"]
    assert "history" not in delta
    assert not delta["logs_reset"]
    controller.history.append({"id": "a" * 32})
    changed = controller.state(delta["log_cursor"], first["history_revision"])
    assert "history" in changed and changed["logs"] == []
    reset = controller.state(controller.sequence + 1000, first["history_revision"])
    assert reset["logs_reset"] and reset["logs"] == list(controller.logs)
    for i in range(610):
        controller.log(str(i))
    assert controller.state(first["log_cursor"])["logs_reset"]
    assert len(controller.logs) == 600


@pytest.mark.parametrize("bad_row", [{"id": "bad id", "kind": "zhidao"}, None])
def test_partial_sync_preserves_other_categories_and_invalid_rows(controller, bad_row):
    controller.event({"type": "courses", "kinds": ["zhidao"], "courses": [
        {"id": "new", "kind": "zhidao"}, bad_row]})
    assert {course_key(c) for c in controller.courses} == {
        "zhidao:new", "zhidao:first", "zhidao:second", "hike:12"}


def test_complete_sync_replaces_only_successful_category(controller):
    controller.event({"type": "courses", "kinds": ["zhidao"], "courses": [
        {"id": "new", "kind": "zhidao"}, {"id": "new", "kind": "zhidao"}]})
    assert [course_key(c) for c in controller.courses] == ["zhidao:new", "hike:12"]
    controller.event({"type": "courses", "kinds": ["zhidao"], "incomplete_kinds": ["zhidao"], "courses": []})
    assert [course_key(c) for c in controller.courses] == ["zhidao:new", "hike:12"]


def test_result_events_finalize_once_and_archive_redacted_logs(controller):
    controller.dispatch({"action": "ai_settings", "ai": {"openai": {"api_key": "private-ai-key"}}})
    worker = start(controller)
    task_id = controller.task_id
    worker.emit({"type": "log", "message": "failure private-ai-key"})
    worker.emit({"type": "qr", "image": "data:image/png;base64,AA=="})
    worker.emit({"type": "course", "key": "zhidao:first", "status": "done"})
    worker.finish(result="completed private-ai-key")
    worker.on_exit(0)
    assert controller.status == "done"
    assert controller.qr is None and controller.task_id is None and controller.process is None
    assert len(controller.history.rows) == 1
    assert controller.history.rows[-1]["has_log"]
    assert not controller.store.exists("active-task.json")
    text = controller.task_log(task_id)["text"]
    assert "[已隐藏]" in text and "private-ai-key" not in text
    restored = Controller(data_dir=controller.local)
    try:
        assert restored.task_log(task_id)["text"] == text
        assert "private-ai-key" not in json.dumps(restored.state())
    finally:
        restored.close()


@pytest.mark.parametrize("result,code", [(None, 0), (None, 1), ("premature success", 1)])
def test_missing_result_or_nonzero_exit_cannot_report_success(controller, result, code):
    worker = start(controller)
    worker.finish(code, result=result)
    assert controller.status == "error"
    assert controller.courses[0]["status"] == "attention"
    assert controller.history.rows[-1]["status"] == "error"


def test_spawn_failure_finalizes_active_record_without_leaking_error(controller):
    controller.worker_factory = Mock(side_effect=RuntimeError("secret private-stacktrace"))
    start(controller)
    assert controller.status == "error" and controller.process is None
    assert not controller.store.exists("active-task.json")
    assert "private-stacktrace" not in json.dumps(controller.state())


def test_worker_error_exit_preserves_structured_error_message(controller):
    worker = start(controller)
    worker.emit({"type": "result", "error": True, "reason": "verification", "message": "需要手动验证，本批次已停止。"})
    worker.finish(1)
    assert controller.status == "error"
    assert controller.message == "需要手动验证，本批次已停止。"
    assert controller.history.rows[-1]["reason"] == "verification"


def test_stop_request_is_nonblocking_child_cleanup_result_is_preserved(controller):
    worker = start(controller)
    task_id = controller.task_id
    before = time.monotonic()
    controller.expire(controller.job_id)
    assert time.monotonic() - before < 0.5
    assert controller.stop_requested == "limited" and controller.status == "running"
    worker.request_stop.assert_called_once_with("limited")
    worker.terminate.assert_not_called()
    controller.stop()
    worker.request_stop.assert_called_once()
    worker.emit({"type": "result", "status": "limited", "message": "cleanup finished", "error": False})
    worker.finish()
    assert controller.status == "limited" and controller.stop_requested is None
    assert "cleanup finished" in controller.task_log(task_id)["text"]
    assert controller.watchdog is None


def test_unresponsive_worker_forced_after_grace_and_stale_timers_are_safe(controller, monkeypatch):
    monkeypatch.setattr("killzhs.app.controller.STOP_GRACE_SECONDS", 0.02)
    worker = start(controller)
    first = controller.job_id
    controller.expire(first)
    limit = time.monotonic() + 2
    while controller.status == "running" and time.monotonic() < limit:
        time.sleep(0.005)
    with controller.lock:
        assert controller.status == "limited" and not worker.is_alive()
        assert controller.history.rows[-1]["status"] == "limited"
    replacement = start(controller)
    controller.expire(first)
    controller.force_stop_after(first, "limited")
    worker.emit({"type": "result", "error": True, "message": "stale event"})
    worker.on_exit(1)
    assert controller.status == "running" and controller.process is replacement
    assert controller.stop_requested is None


def test_stop_signal_failure_forces_exit_and_kills_after_terminate_timeout(controller):
    worker = start(controller)
    worker.request_stop.side_effect = OSError("private-error")
    worker.terminate.side_effect = None
    controller.stop()
    worker.terminate.assert_called_once()
    worker.kill.assert_called_once()
    assert controller.status == "stopped"
    assert not worker.is_alive()


def test_termination_joins_outside_lock_and_rejects_late_events(controller):
    worker = start(controller)

    def terminate():
        worker.emit({"type": "result", "error": True, "message": "late failure"})
        worker.emit({"type": "course", "key": "zhidao:first", "status": "done"})
        acquired = []
        thread = threading.Thread(target=lambda: (controller.lock.acquire(), acquired.append(True), controller.lock.release()))
        thread.start()
        thread.join(0.5)
        assert acquired
        worker.finish(-15)

    worker.terminate.side_effect = terminate
    controller.stop(force=True)
    assert controller.status == "stopped"
    assert controller.courses[0]["status"] == "stopped"
    assert "late failure" not in controller.message


def test_cookie_handoff_remember_and_session_survive_without_state_leaks(controller):
    cookie = [{"name": "CASLOGC", "value": "private-cookie"}]
    controller.event({"type": "cookies", "cookies": cookie})
    assert controller.store.load("cookies.json", []) == cookie
    worker = start(controller)
    assert worker.payload["cookies"] == cookie
    assert "private-cookie" not in json.dumps(controller.state())
    worker.finish(result="done")
    controller.dispatch({"action": "settings", "settings": {"remember": False}})
    assert not controller.store.exists("cookies.json") and controller.has_session()
    worker = start(controller)
    worker.emit({"type": "cookies", "cookies": [{"value": "next-private-cookie"}]})
    assert not controller.store.exists("cookies.json")
    worker.finish(result="done")
    restored = Controller(data_dir=controller.local)
    try:
        assert not restored.cookies and not restored.has_session()
    finally:
        restored.close()


def test_unexpected_exit_recovers_interrupted_history(controller):
    task_id = "a" * 32
    controller.task_logs.append(task_id, {"time": "now", "level": "info", "message": "before crash"})
    controller.history.mark_active({"id": task_id, "started_at": time.time() - 10, "action": "start",
                                    "origin": "schedule", "status": "running", "courses": ["course"]})
    restored = Controller(data_dir=controller.local)
    try:
        assert restored.history.rows[-1]["status"] == "interrupted"
        assert "before crash" in restored.task_log(task_id)["text"]
        assert not restored.store.exists("active-task.json")
    finally:
        restored.close()


def test_detailed_log_path_must_belong_to_history(controller):
    for task_id in ("../config.json", "", "a" * 32):
        with pytest.raises(InputError):
            controller.task_log(task_id)


def test_notification_report_stays_bound_to_original_task(controller):
    controller.dispatch({"action": "notification_settings", "notifications": {
        "enabled": True, "pushplus_token": "private-notification-token"}})
    entered, release = threading.Event(), threading.Event()

    def sender(*_):
        entered.set()
        release.wait(3)

    controller.notifier.sender = sender
    first = start(controller)
    first_id = controller.task_id
    first.finish(result="done")
    assert entered.wait(1)
    start(controller)
    second_id = controller.task_id
    release.set()
    controller.notifier.queue.join()
    assert "已提交到推送服务" in controller.task_log(first_id)["text"]
    assert "已提交到推送服务" not in controller.task_log(second_id)["text"]
    controller.notification_report("skip result", "info", None)


def test_finished_auth_and_skipped_notifications_are_queued(controller, monkeypatch):
    notify = Mock()
    monkeypatch.setattr(controller, "notify", notify)
    worker = start(controller)
    worker.finish(result="done")
    assert notify.call_args.args[0] == "finished"
    controller.dispatch({"action": "refresh"})
    worker = controller.process
    worker.emit({"type": "auth", "logged_in": False})
    worker.emit({"type": "result", "error": True, "message": "登录过期"})
    worker.finish()
    assert notify.call_args.args[0] == "auth"
    controller.dispatch({"action": "plan", "plan": {"enabled": True, "courses": ["hike:12"], "time": "10:00"}})
    now = datetime(2026, 10, 19, 10, 0, 5, tzinfo=timezone(timedelta(hours=8)))
    controller.schedule_tick(now)
    assert controller.history.rows[-1]["status"] == "skipped"
    assert notify.call_args.args[0] == "skipped"


def test_due_plan_budget_once_and_catchup_attempt_survive_restart(controller):
    controller.persist_courses()
    controller.event({"type": "cookies", "cookies": [{"value": "private-cookie"}]})
    controller.dispatch({"action": "plan", "plan": {"enabled": True, "courses": ["hike:12"],
                                                     "time": "10:00", "minutes": 30, "catchup_minutes": 120}})
    now = datetime(2026, 10, 19, 11, 0, tzinfo=timezone(timedelta(hours=8)))
    controller.schedule_tick(now)
    assert controller.origin == "schedule"
    assert controller.deadline - controller.started_at == 1800
    controller.process.finish(result="done")
    assert not controller.schedule_tick(now)
    restored = Controller(data_dir=controller.local, worker_factory=FakeWorker)
    try:
        assert restored.last_schedule_date == now.date().isoformat()
        assert not restored.schedule_tick(now)
        assert len(restored.history.rows) == 1
    finally:
        restored.close()
    controller.last_schedule_date = None
    controller.plan["catchup_minutes"] = 0
    controller.schedule_tick(now)
    assert controller.history.rows[-1]["status"] == "skipped"


def test_verification_targets_current_course_excludes_completed_and_is_detached(controller):
    worker = start(controller, ["zhidao:first", "zhidao:second", "hike:12"])
    worker.emit({"type": "course", "key": "zhidao:first", "status": "done"})
    worker.emit({"type": "course", "key": "zhidao:second", "status": "attention"})
    worker.emit({"type": "result", "reason": "verification", "error": True, "message": "需要手动验证。"})
    verification = controller.state()["verification"]
    assert verification["course_keys"] == ["zhidao:second", "hike:12"]
    assert verification["course_name"] == "第二门课"
    assert verification["url"].endswith("recruitAndCourseId=second")
    verification["course_keys"].clear()
    assert len(controller.verification["course_keys"]) == 2
    worker.finish()


@pytest.mark.parametrize("identifier, encoded", [("first", "first"), ("shared_course=", "shared_course%3D")])
def test_verification_survives_restart_rebuilds_url_and_rejects_ambiguous_names(controller, identifier, encoded):
    controller.courses[0]["id"] = identifier
    controller.persist_courses()
    worker = start(controller, ["zhidao:" + identifier])
    worker.emit({"type": "result", "reason": "verification", "error": True, "message": "需要手动验证。"})
    controller.verification["url"] = "javascript:invalid"
    worker.finish()
    restored = Controller(data_dir=controller.local)
    try:
        assert restored.verification["url"] == "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=" + encoded
        assert restored.verification["course_keys"] == ["zhidao:" + identifier]
    finally:
        restored.close()
    atomic_json(controller.local / "courses.json", [{"id": ident, "kind": "zhidao", "name": "重复"}
                                                  for ident in ("one", "two")])
    atomic_json(controller.local / "history.json", [{"action": "start", "status": "error",
                                                    "message": "需要手动验证", "courses": ["重复"]}])
    restored = Controller(data_dir=controller.local)
    try:
        assert restored.verification["url"] == "https://www.zhihuishu.com/"
        assert not restored.verification["direct"] and restored.verification["course_keys"] == []
    finally:
        restored.close()


def test_verification_blocks_schedule_until_explicit_restart(controller):
    controller.verification = controller.verification_context(["zhidao:first"], "zhidao:first")
    with pytest.raises(InputError, match="仍需手动验证"):
        controller.dispatch({"action": "start", "courses": ["zhidao:first"]}, scheduled=True)
    start(controller)
    assert controller.verification is None


@pytest.mark.parametrize("action", ["assessment_scan", "assessment_run"])
def test_assessment_verification_preserves_kind_and_completed_video_course(controller, action):
    controller.courses[0]["status"] = "done"
    controller.action_request("ai_settings", {"ai": {"enabled": True}})
    controller.assessments = [{"course_key": "zhidao:first", "id": "paper", "exam_id": "exam",
                               "kind": "chapter", "state": 1}]
    controller.dispatch({"action": action, "courses": ["zhidao:first"],
                         "assessments": ["zhidao:first:chapter:paper:exam"]})
    worker = controller.process
    worker.emit({"type": "assessment_course", "key": "zhidao:first"})
    worker.emit({"type": "result", "reason": "verification", "error": True, "message": "平台要求网页验证。"})
    assert controller.verification["action"] == action
    assert controller.verification["course_keys"] == ["zhidao:first"]
    assert controller.courses[0]["status"] == "done"
    worker.finish()


def test_probe_confirmation_is_bound_to_task_probe_deduplicated_and_notifies_once(controller, monkeypatch):
    monkeypatch.setenv("KILLZHS_VERIFY_PROBE", "1")
    notify = Mock()
    monkeypatch.setattr(controller, "notify", notify)
    worker = start(controller)
    task_id = controller.task_id
    event = {"type": "verification_probe", "phase": "waiting", "task_id": task_id, "probe_id": "e" * 32}
    worker.emit(event)
    worker.emit({**event, "phase": "baseline", "baseline": None})
    assert controller.state()["verification_probe"]["awaiting_confirmation"]
    for task, probe in (("old", "e" * 32), (task_id, "f" * 32)):
        with pytest.raises(InputError):
            controller.dispatch({"action": "confirm_verification", "task_id": task, "probe_id": probe})
    request = {"action": "confirm_verification", "task_id": task_id, "probe_id": "e" * 32}
    controller.dispatch(request)
    worker.emit(event)  # Repeated waiting event must not permit another replay.
    with pytest.raises(InputError):
        controller.dispatch(request)
    worker.confirm_verification.assert_called_once_with("e" * 32)
    assert notify.call_count == 1
    worker.emit({"type": "result", "error": True, "reason": "verification", "message": "需要验证。"})
    worker.finish()
    assert notify.call_count == 1
    assert not controller.verification_probe["awaiting_confirmation"]


def test_probe_ignores_stale_events_and_observation_clears_verification(controller, monkeypatch):
    monkeypatch.setenv("KILLZHS_VERIFY_PROBE", "1")
    worker = start(controller)
    event = {"type": "verification_probe", "phase": "waiting", "task_id": controller.task_id, "probe_id": "f" * 32}
    worker.emit({**event, "task_id": "old"})
    worker.emit({**event, "probe_id": "invalid"})
    assert controller.verification_probe is None
    worker.emit(event)
    worker.emit({**event, "phase": "observing"})
    assert controller.verification is None
    assert not controller.verification_probe["awaiting_confirmation"]
    controller.stop()
    with pytest.raises(InputError):
        controller.dispatch({"action": "confirm_verification", "task_id": controller.task_id, "probe_id": "f" * 32})


def test_demo_is_isolated_from_supplied_data_and_runs_scan_answers(tmp_path):
    sentinel = tmp_path / "cookies.json"
    sentinel.write_text("private-user-data", encoding="utf-8")
    instance = Controller(data_dir=tmp_path, demo=True)
    try:
        assert instance.local != tmp_path
        instance.dispatch({"action": "assessment_scan", "courses": ["zhidao:demo01"]})
        instance.thread.join(2)
        assert instance.status == "done" and len(instance.assessments) == 2
        from killzhs.app.actions import item_key
        instance.dispatch({"action": "ai_settings", "ai": {"enabled": True}})
        instance.dispatch({"action": "assessment_run", "courses": ["zhidao:demo01"],
                           "assessments": [item_key(row) for row in instance.assessments]})
        instance.thread.join(2)
        assert all(row["state"] == "submitted" for row in instance.assessment_results.values())
        assert instance.ai_requests == 0
    finally:
        instance.close()
    assert sentinel.read_text(encoding="utf-8") == "private-user-data"
    assert list(tmp_path.iterdir()) == [sentinel]
