"""Cooperative cancellation shared by the worker, the platform client and demo mode."""

import threading
import time
from contextlib import contextmanager

STOP_GRACE_SECONDS = 30


class TaskStopped(BaseException):
    """A BaseException so broad ``except Exception`` handlers never swallow a stop."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class TaskControl:
    """Raises :class:`TaskStopped` once a stop is requested or a deadline passes.

    ``stop_signal`` is a callable returning ``"stopped"``, ``"limited"`` or a
    falsy value. ``deadline`` is a wall-clock timestamp; it is converted to the
    monotonic clock once, so later clock changes cannot shorten or extend a run.
    """

    def __init__(self, stop_signal=None, deadline=None):
        self.stop_signal = stop_signal
        self.deadline = time.monotonic() + max(0, deadline - time.time()) if deadline is not None else None
        self.course_deadline = None
        self.local = threading.local()

    def begin_course(self, minutes):
        self.course_deadline = time.monotonic() + minutes * 60 if minutes else None

    @property
    def cleaning(self):
        return getattr(self.local, "cleaning", False)

    def check(self):
        if self.cleaning:
            return
        reason = self.stop_signal() if self.stop_signal else None
        if reason:
            raise TaskStopped("limited" if reason == "limited" else "stopped")
        now = time.monotonic()
        if self.deadline is not None and now >= self.deadline:
            raise TaskStopped("limited")
        if self.course_deadline is not None and now >= self.course_deadline:
            raise TaskStopped("course_limit")

    @contextmanager
    def cleanup(self):
        """Let a final progress report through after a stop was requested."""
        previous = self.cleaning
        self.local.cleaning = True
        try:
            yield
        finally:
            self.local.cleaning = previous
