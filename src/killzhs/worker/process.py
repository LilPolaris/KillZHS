"""One spawn-based transport for source runs and frozen Windows executables."""

import copy
import multiprocessing
import queue
import threading
from pathlib import Path

from .protocol import validate_event


def _entry(payload, directory, connection, stop_event, stop_reason, confirmations):
    # Never send raw console output or exception tracebacks through the pipe.
    import contextlib
    import os

    from .jobs import execute

    send_lock = threading.Lock()

    def emit(event_type, **values):
        event = {"type": event_type, **values}
        if validate_event(event):
            with send_lock:
                connection.send(event)

    def stopped():
        return ("limited" if stop_reason.value == 2 else "stopped") if stop_event.is_set() else None

    def confirmed():
        try:
            return confirmations.get_nowait()
        except queue.Empty:
            return None

    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            code = execute(payload, Path(directory), emit=emit, stop_signal=stopped, confirmed=confirmed)
    finally:
        connection.close()
        confirmations.close()
    raise SystemExit(code)


class WorkerProcess:
    def __init__(self, payload, data_dir, on_event, on_exit):
        context = multiprocessing.get_context("spawn")
        self.payload = copy.deepcopy(payload)
        self.directory = str(data_dir)
        self.on_event, self.on_exit = on_event, on_exit
        self.receive, self.send = context.Pipe(duplex=False)
        self.stop_event = context.Event()
        self.stop_reason = context.Value("i", 0)
        self.confirmations = context.Queue()
        self.process = context.Process(target=_entry, args=(
            self.payload, self.directory, self.send, self.stop_event, self.stop_reason, self.confirmations,
        ), name="KillZHS-job")
        self.thread = None

    @property
    def pid(self):
        return self.process.pid

    @property
    def exitcode(self):
        return self.process.exitcode

    def start(self):
        try:
            self.process.start()
        except BaseException:
            self.receive.close()
            self.send.close()
            self.confirmations.close()
            raise
        self.send.close()
        # Credentials only need to remain in the child after spawn serializes them.
        self.payload.clear()
        self.thread = threading.Thread(target=self._read, name="KillZHS-events", daemon=True)
        self.thread.start()
        return self

    def _read(self):
        try:
            while True:
                try:
                    event = self.receive.recv()
                except (EOFError, OSError):
                    break
                if validate_event(event):
                    self.on_event(event)
        finally:
            self.receive.close()
            self.process.join()
            self.confirmations.close()
            self.confirmations.join_thread()
            self.on_exit(self.exitcode)

    def request_stop(self, reason="stopped"):
        self.stop_reason.value = 2 if reason == "limited" else 1
        self.stop_event.set()

    def confirm_verification(self, probe_id=None):
        if isinstance(probe_id, str):
            self.confirmations.put(probe_id)

    def terminate(self):
        if self.process.is_alive():
            self.process.terminate()

    def kill(self):
        if self.process.is_alive():
            self.process.kill()

    def is_alive(self):
        return self.process.is_alive()

    def join(self, timeout=None):
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        elif self.process.pid is not None:
            self.process.join(timeout)
