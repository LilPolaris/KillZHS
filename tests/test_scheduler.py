"""Durable daily timing without workers, accounts, sleeps or network calls."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from killzhs.app.history import History, TaskLogs
from killzhs.app.scheduler import Scheduler
from killzhs.app.store import Store
from killzhs.models import InputError, parse_plan

NOW = datetime(2026, 10, 19, 10, 0, 5, tzinfo=timezone(timedelta(hours=8)))


class Controller:
    def __init__(self, directory, *, error=None):
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.status = "idle"
        self.task_id = None
        self.plan = parse_plan({"enabled": True, "time": "10:00", "courses": ["hike:12"],
                                "minutes": 30}, {"hike:12"}).model_dump()
        self.store = Store(directory)
        self.history = History(self.store, TaskLogs(directory))
        self.calls, self.logs, self.notifications = [], [], []
        self.error = error

    def dispatch(self, data, **kwargs):
        # A crash in dispatch cannot cause this day's plan to start again.
        assert self.store.load(Scheduler.FILE, {}) == {"date": NOW.date().isoformat()}
        self.calls.append((data, kwargs))
        if self.error:
            raise self.error

    def log(self, *args):
        self.logs.append(args)

    def notify(self, *args):
        self.notifications.append(args)


def test_due_plan_persists_before_start_and_never_repeats_after_restart(tmp_path):
    controller = Controller(tmp_path)
    scheduler = Scheduler(controller, controller.store)
    assert scheduler.tick(NOW)
    assert controller.calls == [({"action": "start", "courses": ["hike:12"]}, {"scheduled": True, "budget": 30})]
    assert scheduler.last_date == NOW.date().isoformat()
    assert not scheduler.tick(NOW + timedelta(seconds=5))
    restored = Controller(tmp_path)
    other = Scheduler(restored, restored.store)
    assert other.last_date == scheduler.last_date
    assert not other.tick(NOW + timedelta(seconds=10)) and not restored.calls


@pytest.mark.parametrize("reason", ["请先扫码登录。", "课程已变化。", "需要先手动完成验证。", "请记住登录状态。"])
def test_blocked_start_is_recorded_and_not_retried(tmp_path, reason):
    controller = Controller(tmp_path, error=InputError(reason))
    scheduler = Scheduler(controller, controller.store)
    assert scheduler.tick(NOW)
    row = controller.history.rows[-1]
    assert row["status"] == "skipped" and row["origin"] == "schedule" and row["message"] == reason
    assert row["started_at"] == row["ended_at"] == NOW.timestamp()
    assert controller.notifications[-1][0] == "skipped" and controller.logs[-1][1] == "warning"
    assert not scheduler.tick(NOW + timedelta(seconds=1)) and len(controller.calls) == 1


def test_busy_skip_keeps_the_current_task_log(tmp_path):
    controller = Controller(tmp_path)
    controller.status, controller.task_id = "running", "a" * 32
    controller.history.logs.append(controller.task_id, {"time": "now", "level": "info", "message": "current task"})
    scheduler = Scheduler(controller, controller.store)
    assert scheduler.tick(NOW)
    assert not controller.calls and "正在运行" in controller.history.rows[-1]["message"]
    assert controller.history.logs.exists(controller.task_id)
    assert controller.status == "running"


@pytest.mark.parametrize("late,window,starts", [
    (0, 0, True), (59, 0, True), (60, 0, False), (60, 1, True),
    (61, 1, False), (3600, 120, True), (7200, 120, True), (7201, 120, False),
])
def test_first_minute_and_catchup_boundaries(tmp_path, late, window, starts):
    controller = Controller(tmp_path)
    controller.plan["catchup_minutes"] = window
    scheduler = Scheduler(controller, controller.store)
    now = NOW.replace(second=0) + timedelta(seconds=late)
    assert scheduler.tick(now)
    assert bool(controller.calls) is starts
    if not starts:
        assert "错过" in controller.history.rows[-1]["message"]
    assert not scheduler.tick(now)


@pytest.mark.parametrize("condition", ["disabled", "weekday", "early", "closed", "scheduler_closed"])
def test_non_due_plans_do_not_consume_attempt(tmp_path, condition):
    controller = Controller(tmp_path)
    scheduler = Scheduler(controller, controller.store)
    now = NOW
    if condition == "disabled":
        controller.plan["enabled"] = False
    elif condition == "weekday":
        controller.plan["weekdays"] = [1]
    elif condition == "early":
        now -= timedelta(minutes=1)
    elif condition == "closed":
        controller.closed.set()
    else:
        scheduler.close()
    assert not scheduler.tick(now)
    assert scheduler.last_date is None and not controller.calls
    assert not controller.store.exists(Scheduler.FILE)


def test_persistence_failure_prevents_start_but_allows_later_retry(tmp_path, monkeypatch):
    controller = Controller(tmp_path)
    scheduler = Scheduler(controller, controller.store)
    original_save = controller.store.save

    def fail(*_):
        raise OSError("disk error")

    monkeypatch.setattr(controller.store, "save", fail)
    with pytest.raises(OSError):
        scheduler.tick(NOW)
    assert scheduler.last_date is None and not controller.calls
    monkeypatch.setattr(controller.store, "save", original_save)
    assert scheduler.tick(NOW)


def test_concurrent_ticks_dispatch_once(tmp_path):
    controller = Controller(tmp_path)
    scheduler = Scheduler(controller, controller.store)
    with ThreadPoolExecutor(max_workers=4) as workers:
        attempts = list(workers.map(lambda _: scheduler.tick(NOW), range(12)))
    assert sum(attempts) == 1 and len(controller.calls) == 1


def test_next_run_uses_saved_attempts_weekdays_and_catchup(tmp_path):
    controller = Controller(tmp_path)
    controller.plan["weekdays"] = [0]
    controller.plan["catchup_minutes"] = 120
    scheduler = Scheduler(controller, controller.store)
    assert scheduler.next_run(NOW + timedelta(hours=1)) == (NOW + timedelta(hours=1)).isoformat()
    scheduler.tick(NOW)
    assert datetime.fromisoformat(scheduler.next_run(NOW)).date() == NOW.date() + timedelta(days=7)


@pytest.mark.parametrize("state", [[], None, {"date": []}, {"date": "not-a-date"}, {"date": "20261019"}])
def test_malformed_schedule_state_is_ignored(tmp_path, state):
    controller = Controller(tmp_path)
    controller.store.save(Scheduler.FILE, state)
    scheduler = Scheduler(controller, controller.store)
    assert scheduler.last_date is None


def test_background_loop_reports_disk_errors_and_continues_without_secrets(tmp_path, monkeypatch):
    controller = Controller(tmp_path)
    scheduler = Scheduler(controller, controller.store)
    waits, calls = iter([False, False, True]), []
    scheduler.stopped = SimpleNamespace(wait=lambda _: next(waits))

    def tick():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("private-token /private/path")

    monkeypatch.setattr(scheduler, "tick", tick)
    scheduler._run()
    assert len(calls) == 2 and len(controller.logs) == 1
    assert "private" not in str(controller.logs) and controller.logs[0][1] == "error"


def test_background_lifecycle_is_idempotent_and_close_wakes_thread(tmp_path):
    controller = Controller(tmp_path)
    scheduler = Scheduler(controller, controller.store)
    scheduler.start()
    thread = scheduler.thread
    scheduler.start()
    assert scheduler.thread is thread and thread.daemon
    scheduler.close()
    assert not thread.is_alive()
    scheduler.close()
    scheduler.start()
    assert scheduler.thread is thread
