"""Task history (last 100 runs) and bounded per-task text logs."""

import math
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from ..privacy import redact

TASK_ID = re.compile(r"[0-9a-f]{32}")
MAX_LOG_BYTES = 2 * 1024 * 1024
TRUNCATED = "\n[日志已达到 2 MB 上限，后续详细日志不再保存。]\n"
HISTORY_LIMIT = 100


def new_task_id():
    return uuid4().hex


class TaskLogs:
    """One text file per task. Only opaque task IDs can select a file."""

    def __init__(self, directory, *, secrets=(), writable=True):
        self.directory = Path(directory) / "task-logs"
        self.secrets = secrets
        self.writable = writable
        self.lock = threading.RLock()

    def path(self, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("任务记录不存在。")
        return self.directory / (task_id + ".log")

    def exists(self, task_id):
        return self.path(task_id).is_file()

    def append(self, task_id, row):
        path = self.path(task_id)
        if not self.writable:
            return
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            size = path.stat().st_size if path.exists() else 0
            if size >= MAX_LOG_BYTES:
                return
            line = (redact(f"{row['time']} [{row['level']}] {row['message']}", self.secrets) + "\n").encode("utf-8")
            marker = TRUNCATED.encode("utf-8")
            with path.open("ab") as stream:
                if size + len(line) > MAX_LOG_BYTES - len(marker):
                    remaining = MAX_LOG_BYTES - size
                    stream.write(marker[:remaining])
                    stream.write(b" " * max(0, MAX_LOG_BYTES - stream.tell()))
                else:
                    stream.write(line)

    def read(self, task_id):
        with self.path(task_id).open("rb") as stream:
            text = stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace").rstrip()
        return {"task_id": task_id, "text": text, "truncated": TRUNCATED.strip() in text}

    def prune(self, retained):
        if not self.writable:
            return
        with self.lock:
            if self.directory.is_dir():
                for path in self.directory.glob("*.log"):
                    if TASK_ID.fullmatch(path.stem) and path.stem not in retained:
                        path.unlink(missing_ok=True)


class History:
    """Rows are plain dicts; ``revision`` changes whenever the list changes."""

    FILE = "history.json"
    ACTIVE = "active-task.json"

    def __init__(self, store, logs):
        self.store, self.logs = store, logs
        rows = store.load(self.FILE, [])
        self.rows = [row for row in rows if isinstance(row, dict)][-HISTORY_LIMIT:] if isinstance(rows, list) else []
        self.revision = uuid4().hex

    def ids(self):
        return {row["id"] for row in self.rows if isinstance(row.get("id"), str) and TASK_ID.fullmatch(row["id"])}

    def append(self, row, *, keep=()):
        rows = [*self.rows, deepcopy(row)][-HISTORY_LIMIT:]
        self.store.save(self.FILE, rows)
        self.rows = rows
        self.revision = uuid4().hex
        if self.store.writable:
            self.logs.prune(self.ids() | set(keep))

    def latest(self, actions):
        return next((row for row in reversed(self.rows) if row.get("action") in actions), None)

    def mark_active(self, row):
        self.store.save(self.ACTIVE, row)

    def clear_active(self):
        self.store.delete(self.ACTIVE)

    def recover(self):
        """Turn a task left running by a crash into an "interrupted" history row."""
        active = self.store.load(self.ACTIVE, {})
        if not isinstance(active, dict) or not TASK_ID.fullmatch(str(active.get("id", ""))):
            return None
        row = None
        started_at = active.get("started_at")
        if active["id"] not in self.ids() and type(started_at) in (int, float) and math.isfinite(started_at):
            row = {**active, "ended_at": time.time(), "status": "interrupted",
                   "message": "上次程序意外退出，任务已中断；可查看退出前保存的日志。",
                   "has_log": self.logs.exists(active["id"])}
            self.append(row)
        self.clear_active()
        return row
