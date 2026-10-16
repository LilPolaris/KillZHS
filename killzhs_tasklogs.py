"""Bounded, per-task text logs. Only opaque task IDs can select a file."""

from pathlib import Path
import re

MAX_LOG_BYTES = 2 * 1024 * 1024
TRUNCATED = "\n[日志已达到 2 MB 上限，后续详细日志不再保存。]\n"


class TaskLogs:
    def __init__(self, directory):
        self.directory = Path(directory) / "task-logs"

    def path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("任务记录不存在。")
        return self.directory / (task_id + ".log")

    def append(self, task_id, row):
        path = self.path(task_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size if path.exists() else 0
        if size >= MAX_LOG_BYTES:
            return
        line = f"{row['time']} [{row['level']}] {row['message']}\n".encode("utf-8")
        with path.open("ab") as stream:
            if size + len(line) > MAX_LOG_BYTES - len(TRUNCATED.encode("utf-8")):
                stream.write(TRUNCATED.encode("utf-8"))
                # Pad to the cap so subsequent appends cannot grow the file.
                stream.write(b" " * (MAX_LOG_BYTES - stream.tell()))
            else:
                stream.write(line)

    def read(self, task_id):
        path = self.path(task_id)
        with path.open("rb") as stream:
            text = stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace").rstrip()
        return {"task_id": task_id, "text": text, "truncated": TRUNCATED.strip() in text}

    def prune(self, retained):
        if self.directory.is_dir():
            for path in self.directory.glob("*.log"):
                if re.fullmatch(r"[0-9a-f]{32}", path.stem) and path.stem not in retained:
                    path.unlink(missing_ok=True)
