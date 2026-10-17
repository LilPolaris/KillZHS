import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from killzhs.app.history import HISTORY_LIMIT, TRUNCATED, History, TaskLogs, new_task_id
from killzhs.app.store import Store


def test_task_log_survives_restart_and_hides_known_secrets(tmp_path):
    task_id = new_task_id()
    secrets = {"private-ai-key", "private-cookie"}
    logs = TaskLogs(tmp_path, secrets=secrets)
    logs.append(task_id, {"time": "now", "level": "warning", "message": "failure private-ai-key private-cookie"})
    history = History(Store(tmp_path), logs)
    history.append({"id": task_id, "action": "start", "status": "error", "has_log": True})
    restored = History(Store(tmp_path), TaskLogs(tmp_path))
    assert restored.rows == history.rows
    text = restored.logs.read(task_id)["text"]
    assert "private-" not in text and "[已隐藏]" in text
    assert not restored.logs.read(task_id)["truncated"]


def test_unexpected_exit_recovers_once_and_preserves_existing_log(tmp_path):
    store, logs = Store(tmp_path), TaskLogs(tmp_path)
    task_id = "a" * 32
    logs.append(task_id, {"time": "now", "level": "info", "message": "before exit"})
    history = History(store, logs)
    history.mark_active({"id": task_id, "started_at": 100, "action": "start", "origin": "manual"})
    recovered = history.recover()
    assert recovered["status"] == "interrupted" and recovered["has_log"]
    assert recovered["ended_at"] >= 100 and "意外退出" in recovered["message"]
    assert history.rows == [recovered]
    assert not store.exists(history.ACTIVE)
    assert History(store, logs).recover() is None
    assert "before exit" in logs.read(task_id)["text"]


def test_already_archived_active_task_is_cleared_without_duplicate(tmp_path):
    store, logs = Store(tmp_path), TaskLogs(tmp_path)
    history = History(store, logs)
    row = {"id": "a" * 32, "started_at": 100, "status": "done"}
    history.append(row)
    history.mark_active(row)
    assert history.recover() is None
    assert history.rows == [row] and not store.exists(history.ACTIVE)


@pytest.mark.parametrize("value", [None, [], {"id": "../cookies"}, {"id": ["bad"]},
                                   {"id": "a" * 32, "started_at": True},
                                   {"id": "a" * 32, "started_at": "wrong"}])
def test_malformed_active_record_does_not_create_history(tmp_path, value):
    store = Store(tmp_path)
    store.save(History.ACTIVE, value)
    history = History(store, TaskLogs(tmp_path))
    assert history.recover() is None
    assert history.rows == []


def test_history_handles_corrupt_ids_and_retains_latest_runs(tmp_path):
    store, logs = Store(tmp_path), TaskLogs(tmp_path)
    store.save(History.FILE, [{"id": ["corrupt"]}, None, {"id": "b" * 32, "action": "refresh"}])
    history = History(store, logs)
    assert history.ids() == {"b" * 32}
    first_revision = history.revision
    old, active, keep = "b" * 32, "c" * 32, "d" * 32
    row = {"time": "now", "level": "info", "message": "saved"}
    for task_id in (old, active, keep):
        logs.append(task_id, row)
    for number in range(HISTORY_LIMIT):
        history.append({"id": f"{number:032x}", "action": "start", "courses": ["course"]}, keep={active, keep})
    assert len(history.rows) == HISTORY_LIMIT
    assert history.revision != first_revision
    assert not logs.exists(old) and logs.exists(active) and logs.exists(keep)
    assert history.latest({"start"}) == history.rows[-1]
    assert history.latest({"unknown"}) is None
    assert json.loads(store.path(History.FILE).read_text(encoding="utf-8")) == history.rows


def test_append_takes_a_snapshot_and_failed_write_does_not_duplicate_recovery(tmp_path, monkeypatch):
    store, logs = Store(tmp_path), TaskLogs(tmp_path)
    history = History(store, logs)
    row = {"id": "a" * 32, "started_at": 100, "courses": ["original"]}
    history.append(row)
    row["courses"].append("mutated")
    assert history.rows[0]["courses"] == ["original"]
    original_save = store.save
    history.mark_active({"id": "b" * 32, "started_at": 100})
    revision = history.revision

    def fail(*_):
        raise OSError("disk error")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        history.recover()
    assert len(history.rows) == 1 and history.revision == revision
    assert store.exists(history.ACTIVE)
    monkeypatch.setattr(store, "save", original_save)
    assert history.recover()["id"] == "b" * 32
    assert len(history.rows) == 2


def test_log_capacity_is_bounded_under_concurrent_appends_and_pruning_keeps_unrelated_files(tmp_path, monkeypatch):
    logs = TaskLogs(tmp_path)
    task_id, keep = "a" * 32, "b" * 32
    row = {"time": "now", "level": "info", "message": "文" * 1000}
    monkeypatch.setattr("killzhs.app.history.MAX_LOG_BYTES", 4096)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _: logs.append(task_id, row), range(20)))
    assert logs.path(task_id).stat().st_size <= 4096
    result = logs.read(task_id)
    assert result["truncated"] and result["text"].count(TRUNCATED.strip()) == 1
    logs.append(keep, row)
    unrelated = logs.directory / "manual.log"
    unrelated.write_text("keep", encoding="utf-8")
    logs.prune({keep})
    assert not logs.exists(task_id) and logs.exists(keep) and unrelated.exists()


@pytest.mark.parametrize("value", ["../ai.json", "..", "", "invalid", "A" * 32, None, ["a"]])
def test_only_opaque_ids_can_select_log_files(tmp_path, value):
    with pytest.raises(ValueError):
        TaskLogs(tmp_path).path(value)


def test_readonly_history_does_not_prune_existing_logs(tmp_path):
    logs = TaskLogs(tmp_path)
    task_id = "a" * 32
    logs.append(task_id, {"time": "now", "level": "info", "message": "keep"})
    history = History(Store(tmp_path, writable=False), logs)
    history.append({"id": "b" * 32})
    assert logs.exists(task_id)
    readonly = TaskLogs(tmp_path, writable=False)
    readonly.append(task_id, {"time": "now", "level": "info", "message": "not written"})
    readonly.prune(set())
    assert "not written" not in readonly.read(task_id)["text"]
