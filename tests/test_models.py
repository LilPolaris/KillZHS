"""Offline validation and durable preferences, ported from the workbench tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from killzhs.app.store import Preferences, Store, atomic_json, load_courses, load_json, save_courses
from killzhs.models import (
    AIConfig,
    Course,
    InputError,
    NotificationConfig,
    Plan,
    RunSettings,
    api_url,
    parse_plan,
    parse_settings,
    public_ai,
    public_notifications,
    unique_courses,
    update_ai,
    update_notifications,
)


@pytest.mark.parametrize("settings", [
    {"speed": float("nan")}, {"speed": float("inf")}, {"speed": True}, {"limit": -1},
    {"limit": 0.5}, {"remember": "yes"}, {"threshold": 0}, {"budget_minutes": 241},
    {"command": "arbitrary"}, [], None,
])
def test_settings_reject_invalid_nonfinite_and_unknown_values(settings):
    with pytest.raises(InputError):
        parse_settings(settings)


def test_settings_defaults_and_limits():
    assert parse_settings({}) == RunSettings()
    assert parse_settings({"speed": 16, "limit": 1440, "threshold": 2, "budget_minutes": 240}).speed == 16


def test_ai_partial_update_preserves_keys_and_public_state_never_contains_them():
    saved = update_ai(AIConfig(), {"openai": {"api_key": "private-model-key"},
                                  "ppt_processing": {"moonShot": {"api_key": "private-ppt-key"}}})
    updated = update_ai(saved, {"openai": {"model_name": "example-model"}})
    assert updated.openai.api_key == "private-model-key"
    assert not saved.openai.model_name
    public = public_ai(updated)
    assert public["openai"]["has_api_key"]
    assert "private-" not in json.dumps(public)
    cleared = update_ai(updated, {"openai": {"api_key": ""}})
    assert not public_ai(cleared)["openai"]["has_api_key"]
    assert public_ai(cleared)["ppt_processing"]["moonShot"]["has_api_key"]
    assert not AIConfig().enabled and AIConfig().no_exam


@pytest.mark.parametrize("value", [
    "file:///private", "https://user:key@example.test", "https://example.test?api_key=secret",
    "http://example.test:bad", "https://example.test\n", "https://example.test#secret", [], None,
])
def test_provider_url_rejects_credentials_queries_and_invalid_addresses(value):
    with pytest.raises(InputError):
        api_url(value)


def test_provider_url_normalizes_only_missing_version_path():
    assert api_url("https://example.test") == "https://example.test/v1"
    assert api_url("https://example.test/custom/v1/") == "https://example.test/custom/v1"


@pytest.mark.parametrize("patch", [
    {"max_requests": value} for value in (0, True, 101, 1.5)
] + [{"enabled": "yes"}, {"openai": {"extra": "x"}}, {"ppt_processing": {"provide_to_ai": 1}}])
def test_ai_rejects_invalid_caps_and_nested_fields(patch):
    with pytest.raises(InputError):
        update_ai(AIConfig(), patch)


@pytest.mark.parametrize("patch", [
    {"enabled": True}, {"events": {"other": True}}, {"events": {"finished": 1}},
    {"bark_server": "https://user:password@example.test"}, {"bark_server": "https://example.test?key=private"},
    {"pushplus_token": "key\n"}, {"enabled": True, "provider": "bark", "pushplus_token": "only-pushplus"},
])
def test_notification_validation(patch):
    with pytest.raises(InputError):
        update_notifications(NotificationConfig(), patch)


def test_notification_partial_updates_and_public_flags():
    saved = update_notifications(NotificationConfig(), {"enabled": True, "pushplus_token": "private-token"})
    updated = update_notifications(saved, {"events": {"finished": False}})
    assert updated.pushplus_token == "private-token"
    assert not updated.events.finished and updated.events.auth
    assert saved.events.finished
    assert "private-token" not in json.dumps(public_notifications(updated))
    assert public_notifications(updated)["has_pushplus_token"]


@pytest.mark.parametrize("row", [None, [], {"id": None}, {"id": True}, {"id": "bad id"},
                                     {"id": "../path"}, {"id": "１２"}, {"id": "12", "kind": "ai"}])
def test_courses_reject_bad_ids_and_ai_without_class(row):
    with pytest.raises(InputError):
        Course.parse(row)


def test_course_keys_and_partial_sync_rows():
    one = Course.parse({"id": 12, "kind": "ai", "class_id": 34})
    two = Course.parse({"id": "12", "kind": "ai", "class_id": "35"})
    assert one.key == "ai:12:34" and two.key == "ai:12:35"
    good = {"id": "shared_01==", "kind": "zhidao", "name": "示例共享课"}
    courses, skipped = unique_courses([good, {**good, "name": "duplicate"}, {"id": "bad id"}])
    assert [course.name for course in courses] == ["示例共享课"]
    assert len(skipped) == 1
    assert one.to_dict()["class_id"] == "34"


@pytest.mark.parametrize("patch", [
    {"enabled": True}, {"time": "25:00"}, {"time": "2０:00"}, {"weekdays": []},
    {"weekdays": [True]}, {"weekdays": [7]}, {"courses": ["hike:unknown"]}, {"minutes": 0},
    {"catchup_minutes": -1}, {"catchup_minutes": 721},
])
def test_plan_validation(patch):
    with pytest.raises(InputError):
        parse_plan(patch, {"hike:12"})


def test_plan_normalizes_duplicates_and_computes_next_run():
    plan = parse_plan({"enabled": True, "time": "10:00", "weekdays": [0, 0],
                       "courses": ["hike:12", "hike:12"]}, {"hike:12"})
    assert plan.courses == ["hike:12"] and plan.weekdays == [0]
    now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    assert datetime.fromisoformat(plan.next_run(now=now)).hour == 10
    assert datetime.fromisoformat(plan.next_run(now.date().isoformat(), now)).date() == now.date() + timedelta(days=7)
    assert Plan().next_run(now=now) is None


def test_atomic_json_roundtrip_and_isolated_fallback(tmp_path):
    path = tmp_path / "nested" / "config.json"
    fallback = {"rows": []}
    loaded = load_json(path, fallback)
    loaded["rows"].append("changed")
    assert fallback == {"rows": []}
    atomic_json(path, {"name": "课程"})
    assert load_json(path, {}) == {"name": "课程"}
    path.write_bytes(b"\xef\xbb\xbf{\"ok\":true}")
    assert load_json(path, {}) == {"ok": True}
    for data in (b"{broken", b"\xff\xfe"):
        path.write_bytes(data)
        assert load_json(path, fallback) == fallback


def test_failed_atomic_replace_keeps_original_and_cleans_its_temporary(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    atomic_json(path, {"old": True})
    original = path.read_bytes()

    def fail(*_):
        raise OSError("disk error")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(path, {"new": True})
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []
    with pytest.raises(ValueError):
        atomic_json(path, {"invalid": float("nan")})
    assert path.read_bytes() == original


def test_concurrent_atomic_writes_each_keep_a_complete_document(tmp_path):
    path = tmp_path / "config.json"
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda number: atomic_json(path, {"number": number, "rows": [number] * 100}), range(16)))
    saved = load_json(path, {})
    assert saved["rows"] == [saved["number"]] * 100
    assert not list(tmp_path.glob("*.tmp"))


def test_preferences_recover_sections_independently_and_roundtrip(tmp_path):
    store = Store(tmp_path)
    store.save("config.json", {"settings": {"speed": False}, "ai": {"max_requests": 3},
                               "notifications": {"provider": "unknown"},
                               "plan": {"enabled": True, "courses": ["hike:12"]}})
    prefs = Preferences(store, {"hike:12"})
    assert prefs.settings == RunSettings() and prefs.notifications == NotificationConfig()
    assert prefs.ai.max_requests == 3 and prefs.plan.enabled
    prefs.ai = update_ai(prefs.ai, {"openai": {"api_key": "offline-key"}})
    prefs.save()
    restored = Preferences(store, {"hike:12"})
    assert restored.ai == prefs.ai and restored.plan == prefs.plan
    assert Preferences(store, set()).plan == Plan()


def test_courses_persist_and_corrupted_rows_are_skipped(tmp_path):
    store = Store(tmp_path)
    course = Course.parse({"id": "12", "kind": "hike"})
    save_courses(store, [course])
    assert load_courses(store) == [course]
    store.save("courses.json", [course.to_dict(), {"id": "bad id"}, None])
    assert load_courses(store) == [course]


def test_readonly_store_never_creates_or_deletes_files(tmp_path):
    directory = tmp_path / "demo"
    store = Store(directory, writable=False)
    Preferences(store, set()).save()
    store.delete("config.json")
    assert not directory.exists()
    directory.mkdir()
    path = directory / "config.json"
    path.write_text("original", encoding="utf-8")
    store.save("config.json", {"replace": True})
    store.delete("config.json")
    assert path.read_text(encoding="utf-8") == "original"
