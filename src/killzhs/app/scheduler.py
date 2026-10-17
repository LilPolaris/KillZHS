"""Daily local-time attempts; persist the attempt before dispatching any work."""

import threading
from datetime import date, datetime

from ..models import InputError, Plan, parse_plan
from .history import new_task_id


class Scheduler:
    """The controller owns execution and notifications, this class owns timing.

    ``controller`` supplies an RLock, a ``closed`` Event, dict ``plan``, task
    ``status``, ``history`` (History), and dispatch/log/notify methods. Dispatch
    performs login, remember-session, verification and course validation through
    the same path as a manual start. A blocked attempt is never retried that day.
    ``tick(now)`` is also usable without starting a background thread.
    """

    FILE = "schedule-state.json"

    def __init__(self, controller, store):
        self.controller, self.store = controller, store
        state = store.load(self.FILE, {})
        last_date = state.get("date") if isinstance(state, dict) else None
        try:
            self.last_date = last_date if isinstance(last_date, str) and date.fromisoformat(last_date).isoformat() == last_date else None
        except ValueError:
            self.last_date = None
        self.thread = None
        self.stopped = threading.Event()

    def _plan(self):
        plan = self.controller.plan
        if isinstance(plan, Plan):
            return plan
        # Deleted courses remain in a saved plan until it is edited; dispatch
        # turns that into a recorded skip instead of killing the scheduler.
        keys = plan.get("courses", []) if isinstance(plan, dict) else []
        known = [key for key in keys if isinstance(key, str)] if isinstance(keys, list) else []
        return parse_plan(plan, known)

    def next_run(self, now=None):
        with self.controller.lock:
            return self._plan().next_run(self.last_date, now)

    def tick(self, now=None):
        """Return whether today's attempt was consumed, whether started or skipped.

        A persistence failure propagates before dispatch and leaves last_date
        unchanged. The periodic loop reports it using a fixed, secret-free text.
        """
        now = now or datetime.now().astimezone()
        today = now.date().isoformat()
        controller = self.controller
        with controller.lock:
            if self.stopped.is_set() or controller.closed.is_set():
                return False
            plan = self._plan()
            if not plan.enabled or now.weekday() not in plan.weekdays or self.last_date == today:
                return False
            due = plan.due_today(now)
            if now < due:
                return False
            self.store.save(self.FILE, {"date": today})
            self.last_date = today
            late = (now - due).total_seconds()
            if late >= 60 and (not plan.catchup_minutes or late > plan.catchup_minutes * 60):
                reason = "已错过计划时间，当天不补跑。"
            elif controller.status == "running":
                reason = "已有任务正在运行，本次定时任务已跳过。"
            else:
                try:
                    controller.dispatch({"action": "start", "courses": list(plan.courses)},
                                        scheduled=True, budget=plan.minutes)
                except InputError as error:
                    reason = str(error)
                else:
                    return True
            controller.log("定时任务未启动：" + reason, "warning")
            row = {"id": new_task_id(), "started_at": now.timestamp(), "ended_at": now.timestamp(),
                   "action": "start", "origin": "schedule", "status": "skipped", "message": reason,
                   "courses": [], "ai_requests": 0, "has_log": False}
            controller.history.append(row, keep={getattr(controller, "task_id", None)})
            controller.notify("skipped", "KillZHS 定时计划已跳过",
                              reason + " 请打开工作台查看计划及登录状态。")
            return True

    def start(self):
        with self.controller.lock:
            if self.stopped.is_set() or self.controller.closed.is_set() or self.thread is not None:
                return
            self.thread = threading.Thread(target=self._run, name="daily-schedule", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stopped.wait(1):
            if self.controller.closed.is_set():
                return
            try:
                self.tick()
            except OSError:
                self.controller.log("无法保存计划运行状态，本次未执行。请检查用户目录权限。", "error")

    def close(self):
        self.stopped.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
