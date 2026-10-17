"""Shared behaviour of the three course runners."""

import logging

log = logging.getLogger("killzhs.platform")


def no_progress(current, total, title, waiting=False):
    pass


class Runner:
    def __init__(self, http, control, *, speed=None, threshold=None, progress=no_progress):
        self.http, self.control = http, control
        self.speed = speed and max(speed, 0.1)  # Falsy means the course type's default speed.
        self.threshold = max(threshold or 0, 0.0) or 0.91
        self.progress = progress

    def check(self):
        if self.control is not None:
            self.control.check()

    def flush_after_stop(self, flush):
        """Report progress that was played but not yet sent, once, after a stop."""
        probe = self.http.probe
        if flush is None or (probe and probe.suppress_stop_flush):
            return
        try:
            with self.control.cleanup():
                flush()
        except Exception:
            log.warning("停止前最后一次进度提交失败，平台可能只保留此前记录。")
