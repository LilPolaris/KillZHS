"""Cooperative task cancellation and bounded progress flushing."""

from contextlib import contextmanager
from functools import wraps
import json
from pathlib import Path
import threading
import time

STOP_GRACE_SECONDS = 30


class TaskStopped(BaseException):
    """Do not let legacy exception/retry handlers swallow a stop request."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class TaskControl:
    def __init__(self, stop_file=None, deadline=None):
        self.stop_file = Path(stop_file) if stop_file else None
        self.deadline = time.monotonic() + max(0, deadline - time.time()) if deadline else None
        self.course_deadline = None
        self.local = threading.local()

    def begin_course(self, minutes):
        self.course_deadline = time.monotonic() + minutes * 60 if minutes else None

    def check(self):
        if getattr(self.local, "cleaning", False):
            return
        if self.stop_file and self.stop_file.is_file():
            try:
                reason = json.loads(self.stop_file.read_text(encoding="utf-8")).get("reason")
            except (OSError, ValueError, AttributeError):
                reason = "stopped"
            raise TaskStopped("limited" if reason == "limited" else "stopped")
        now = time.monotonic()
        if self.deadline is not None and now >= self.deadline:
            raise TaskStopped("limited")
        if self.course_deadline is not None and now >= self.course_deadline:
            raise TaskStopped("course_limit")

    @contextmanager
    def cleanup(self):
        previous = getattr(self.local, "cleaning", False)
        self.local.cleaning = True
        try:
            yield
        finally:
            self.local.cleaning = previous


def finish_on_stop(function):
    @wraps(function)
    def wrapped(client, *args, **kwargs):
        previous = getattr(client, "_stop_flush", None)
        client._stop_flush = None
        try:
            return function(client, *args, **kwargs)
        except TaskStopped:
            flush = client._stop_flush
            if flush is not None:
                try:
                    with client.task_control.cleanup():
                        flush()
                except Exception:
                    from logger import logger
                    logger.warning("停止前最后一次进度提交失败，平台可能只保留此前记录。")
            raise
        finally:
            client._stop_flush = previous
    return wrapped
