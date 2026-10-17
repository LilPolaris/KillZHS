"""Cancellation escapes broad handlers and uses one monotonic deadline."""

import threading

import pytest

from killzhs.runtime import STOP_GRACE_SECONDS, TaskControl, TaskStopped


def test_monotonic_deadline_cleanup_and_course_reset(monkeypatch):
    clock = [10]
    monkeypatch.setattr("killzhs.runtime.time.time", lambda: 100)
    monkeypatch.setattr("killzhs.runtime.time.monotonic", lambda: clock[0])
    control = TaskControl(deadline=105)
    clock[0] = 14
    control.check()
    # Wall-clock corrections cannot extend a running task.
    monkeypatch.setattr("killzhs.runtime.time.time", lambda: -1000)
    clock[0] = 15
    with pytest.raises(TaskStopped, match="limited"):
        control.check()
    with control.cleanup():
        control.check()
        with control.cleanup():
            control.check()
        assert control.cleaning
    with pytest.raises(TaskStopped, match="limited"):
        control.check()
    course = TaskControl()
    course.begin_course(1)
    clock[0] = 76
    with pytest.raises(TaskStopped, match="course_limit"):
        course.check()
    course.begin_course(1)
    course.check()
    course.begin_course(0)
    clock[0] = 10000
    course.check()
    assert STOP_GRACE_SECONDS == 30


@pytest.mark.parametrize("signal,reason", [("stopped", "stopped"), ("limited", "limited"), (True, "stopped")])
def test_stop_signal_escapes_exception_handlers(signal, reason):
    control = TaskControl(lambda: signal)
    with pytest.raises(TaskStopped) as stopped:
        try:
            control.check()
        except Exception:
            pytest.fail("cooperative stop was swallowed")
    assert stopped.value.reason == reason


def test_expired_zero_deadline_is_not_treated_as_disabled():
    with pytest.raises(TaskStopped, match="limited"):
        TaskControl(deadline=0).check()


def test_cleanup_flag_is_thread_local_and_unwinds_after_error():
    control = TaskControl(lambda: "stopped")
    reasons = []

    def check_other_thread():
        try:
            control.check()
        except TaskStopped as stopped:
            reasons.append(stopped.reason)

    with pytest.raises(ValueError), control.cleanup():
        thread = threading.Thread(target=check_other_thread)
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and reasons == ["stopped"]
        control.check()
        raise ValueError("cleanup failed")
    assert not control.cleaning
