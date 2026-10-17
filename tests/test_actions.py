"""Action registry validation and immutable task snapshots."""

import copy
import json
from unittest.mock import Mock

import pytest

from killzhs.app.actions import REGISTRY, course_key, item_key
from killzhs.app.controller import DEFAULTS, Controller
from killzhs.app.store import atomic_json
from killzhs.models import InputError
from tests.test_controller import controller as shared_controller
from tests.test_controller import start


@pytest.fixture(name="controller")
def controller_fixture(tmp_path):
    yield from shared_controller.__wrapped__(tmp_path)


def test_registry_contains_all_frontend_actions_and_named_api(controller):
    assert set(REGISTRY) == {"stop", "confirm_verification", "ai_settings", "notification_settings",
                             "test_notification", "plan", "pause_plan", "settings", "add", "login", "refresh",
                             "start", "test_ai", "diagnose", "assessment_scan", "assessment_run"}
    controller.action_request("settings", {"settings": {"limit": 10}, "action": "login"})
    assert controller.settings["limit"] == 10 and controller.status == "idle"
    for data in (None, [], "settings", {}, {"action": []}, {"action": "unsupported"}):
        with pytest.raises(InputError):
            controller.dispatch(data)
    with pytest.raises(InputError):
        controller.action_request("settings", [])


@pytest.mark.parametrize("settings", [{"speed": False}, {"limit": -1}, {"limit": 1.1}, {"budget_minutes": 241},
                                     {"speed": float("nan")}, {"speed": float("inf")}, {"remember": "yes"},
                                     {"unknown": 1}, []])
def test_invalid_settings_are_rejected_without_changing_state(controller, settings):
    before = copy.deepcopy(controller.settings)
    with pytest.raises(InputError):
        controller.dispatch({"action": "settings", "settings": settings})
    assert controller.settings == before


def test_settings_and_courses_do_not_write_sibling_or_unknown_files(controller, tmp_path):
    untouched = tmp_path / "execution.json"
    untouched.write_text("unrelated content", encoding="utf-8")
    controller.action_request("add", {"course": {"id": "added", "kind": "hike", "name": "课程"}})
    controller.action_request("settings", {"settings": {"limit": 10}})
    stored = controller.store.load("config.json", {})
    assert stored["settings"]["limit"] == 10
    assert all("status" not in c for c in controller.store.load("courses.json", []))
    assert untouched.read_text(encoding="utf-8") == "unrelated content"
    with pytest.raises(InputError, match="已经在清单"):
        controller.action_request("add", {"course": {"id": "added", "kind": "hike"}})


def test_invalid_saved_settings_fall_back_without_losing_other_preferences(tmp_path):
    atomic_json(tmp_path / "config.json", {"settings": {"limit": -1}, "ai": {"max_requests": 2}})
    instance = Controller(data_dir=tmp_path)
    try:
        assert instance.settings == DEFAULTS
        assert instance.ai["max_requests"] == 2
    finally:
        instance.close()


def test_start_requires_login_known_selection_and_installed_dependencies(controller):
    controller.logged_in = False
    with pytest.raises(InputError, match="先登录"):
        start(controller)
    controller.logged_in = True
    for keys in ([], ["invented"], "hike:12", [1], ["hike:12"] * 201):
        with pytest.raises(InputError):
            controller.dispatch({"action": "start", "courses": keys})
    controller.dependencies = ["missing-test-dependency"]
    with pytest.raises(InputError, match="缺少依赖"):
        controller.dispatch({"action": "refresh"})


def test_course_validation_ai_class_ids_and_dedup(controller):
    for course in ({"id": "bad id"}, {"id": "123", "kind": "ai"},
                   {"id": "not-numeric", "kind": "ai", "class_id": "1"},
                   {"id": "123", "kind": "ai", "class_id": "非数字"}):
        with pytest.raises(InputError):
            controller.dispatch({"action": "add", "course": course})
    for class_id in ("4", "5"):
        controller.dispatch({"action": "add", "course": {"id": "123", "kind": "ai", "class_id": class_id}})
    assert {course_key(c) for c in controller.courses} >= {"ai:123:4", "ai:123:5"}
    worker = start(controller, ["hike:12", "hike:12"])
    assert len(worker.payload["courses"]) == 1


def test_ai_saved_during_run_preserves_active_snapshot_and_secrets(controller):
    worker = start(controller)
    started_at = controller.started_at
    controller.action_request("ai_settings", {"ai": {"enabled": True, "max_requests": 2,
                                                       "openai": {"api_key": "private-model-key"}}})
    state = controller.state()
    assert controller.status == "running" and controller.process is worker
    assert controller.started_at == started_at and controller.active_ai_max_requests == 10
    assert state["ai"]["max_requests"] == 2 and state["ai"]["openai"]["has_api_key"]
    saved = json.loads((controller.local / "config.json").read_text(encoding="utf-8"))
    assert saved["ai"]["max_requests"] == 2 and saved["ai"]["enabled"]
    assert "private-model-key" not in json.dumps(state)
    assert not worker.payload["ai"]["enabled"] and worker.payload["ai"]["max_requests"] == 10
    assert worker.payload["ai"]["openai"]["api_key"] == ""
    controller.action_request("ai_settings", {"ai": {"openai": {"model_name": "model"}}})
    assert controller.ai["openai"]["api_key"] == "private-model-key"
    for action in ("refresh", "settings", "add", "assessment_scan"):
        with pytest.raises(InputError, match="正在运行"):
            controller.dispatch({"action": action})


def test_notifications_save_without_delivery_keep_secrets_and_allow_explicit_test(controller, monkeypatch):
    submit = Mock()
    monkeypatch.setattr(controller.notifier, "submit", submit)
    controller.action_request("notification_settings", {"notifications": {"pushplus_token": "private-push-token"}})
    submit.assert_not_called()
    controller.action_request("notification_settings", {"notifications": {"enabled": True}})
    assert controller.notifications["pushplus_token"] == "private-push-token"
    snapshot = controller.state()["notifications"]
    assert snapshot["has_pushplus_token"] and "pushplus_token" not in snapshot
    controller.action_request("test_notification")
    assert submit.call_args.kwargs["test"] is True


def test_plan_pause_does_not_stop_current_task_and_remember_is_required(controller):
    controller.action_request("settings", {"settings": {"remember": False}})
    with pytest.raises(InputError, match="记住登录"):
        controller.action_request("plan", {"plan": {"enabled": True, "courses": ["hike:12"]}})
    controller.action_request("settings", {"settings": {"remember": True}})
    controller.action_request("plan", {"plan": {"enabled": True, "courses": ["hike:12"]}})
    with pytest.raises(InputError, match="暂停定时"):
        controller.action_request("settings", {"settings": {"remember": False}})
    worker = start(controller)
    controller.action_request("pause_plan")
    assert controller.status == "running" and controller.process is worker and not controller.plan["enabled"]


def scan(controller):
    controller.action_request("assessment_scan", {"courses": ["zhidao:first"]})
    rows = [{"course_key": "zhidao:first", "kind": kind, "id": str(index), "exam_id": "paper" + str(index),
             "name": kind, "state": 1} for index, kind in enumerate(("chapter", "exam"))]
    worker = controller.process
    worker.emit({"type": "assessments", "course_key": "zhidao:first", "items": rows})
    worker.finish(result="scanned")
    return rows


def test_assessment_actions_require_scanned_items_and_enabled_ready_ai(controller):
    for keys in (["hike:12"], ["zhidao:first", "zhidao:second"]):
        with pytest.raises(InputError, match="一门共享课"):
            controller.action_request("assessment_scan", {"courses": keys})
    rows = scan(controller)
    keys = [item_key(row) for row in rows]
    data = {"courses": ["zhidao:first"], "assessments": keys}
    with pytest.raises(InputError, match="启用 AI"):
        controller.action_request("assessment_run", data)
    controller.action_request("ai_settings", {"ai": {"enabled": True, "use_zhidao_ai": False}})
    with pytest.raises(InputError, match="准确的模型名称"):
        controller.action_request("assessment_run", data)
    controller.action_request("ai_settings", {"ai": {"use_zhidao_ai": True}})
    for selected in (["invented"], [], "chapter", keys * 26):
        with pytest.raises(InputError):
            controller.action_request("assessment_run", {**data, "assessments": selected})
    rows[0]["state"] = 3
    with pytest.raises(InputError, match="已提交"):
        controller.action_request("assessment_run", data)
    rows[0]["state"] = 1
    controller.action_request("assessment_run", {**data, "assessments": [keys[0], keys[0]]})
    assert len(controller.process.payload["assessments"]) == 1


@pytest.mark.parametrize("reason", ["error", "stopped", "limited"])
def test_failed_assessment_cannot_leave_paper_running(controller, reason):
    rows = scan(controller)
    key = item_key(rows[0])
    controller.action_request("ai_settings", {"ai": {"enabled": True}})
    controller.action_request("assessment_run", {"courses": ["zhidao:first"], "assessments": [key]})
    worker = controller.process
    worker.emit({"type": "assessment_result", "key": key, "state": "running"})
    worker.emit({"type": "result", "message": "模拟网络错误，结果需核对。", "status": reason, "error": reason == "error"})
    worker.finish()
    assert controller.assessment_results[key]["state"] == ("attention" if reason == "error" else reason)
    assert "核对" in controller.assessment_results[key]["message"]


def test_assessment_success_is_not_erased_by_other_failure(controller):
    rows = scan(controller)
    keys = [item_key(row) for row in rows]
    controller.action_request("ai_settings", {"ai": {"enabled": True}})
    controller.action_request("assessment_run", {"courses": ["zhidao:first"], "assessments": keys})
    worker = controller.process
    worker.emit({"type": "assessment_result", "key": keys[0], "state": "submitted"})
    worker.emit({"type": "assessment_result", "key": keys[1], "state": "running"})
    worker.emit({"type": "result", "error": True, "message": "结果需核对。"})
    worker.finish()
    assert controller.assessment_results[keys[0]]["state"] == "submitted"
    assert controller.assessment_results[keys[1]]["state"] == "attention"


def test_ai_course_exam_defaults_skip_and_explicit_exam_requires_ready_ai(controller):
    controller.action_request("add", {"course": {"id": "123", "kind": "ai", "class_id": "4"}})
    assert not controller.ai["enabled"] and controller.ai["no_exam"]
    worker = start(controller, ["ai:123:4"])
    worker.finish(result="done")
    controller.action_request("ai_settings", {"ai": {"no_exam": False}})
    with pytest.raises(InputError, match="尚未启用"):
        start(controller, ["ai:123:4"])
    controller.action_request("ai_settings", {"ai": {"enabled": True, "use_zhidao_ai": False}})
    with pytest.raises(InputError, match="模型名称"):
        start(controller, ["ai:123:4"])


def test_closed_controller_rejects_new_actions(controller):
    controller.close()
    with pytest.raises(InputError, match="正在退出"):
        controller.action_request("refresh")
