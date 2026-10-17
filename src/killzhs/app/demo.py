"""Offline demonstrations using the same callbacks as a real worker."""

import copy
import threading
import time

from ..runtime import TaskControl, TaskStopped
from .actions import course_key, item_key, normalize_course


def demo_courses():
    return [normalize_course({"id": ident, "name": name, "kind": kind}) for ident, name, kind in (
        ("demo01", "艺术与审美", "zhidao"), ("demo02", "大学生心理健康", "zhidao"),
        ("10003", "人工智能与信息社会", "hike"), ("10004", "中国传统文化", "hike"))]


def run_demo(payload, emit, control, *, sleep=time.sleep):
    """Generate page-visible demonstration events, without any external services."""
    action = payload["action"]
    emit({"type": "log", "message": "演示操作：" + action + "，不会连接智慧树。", "level": "info"})
    if action == "assessment_scan":
        key = course_key(payload["courses"][0])
        items = [{"id": "demo" + str(index), "exam_id": "paper" + str(index), "kind": kind,
                  "course_key": key, "name": name, "state": 1, "start": "", "end": "",
                  "url": "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"}
                 for index, kind, name in ((1, "chapter", "第一章 · 章节测验"), (2, "exam", "课程在线考试"))]
        emit({"type": "assessments", "course_key": key, "items": items})
    elif action == "assessment_run":
        for row in payload["assessments"]:
            control.check()
            emit({"type": "assessment_result", "key": item_key(row), "state": "submitted", "answered": 5,
                  "score": None, "message": "演示交卷成功，未连接平台或模型。"})
    elif action == "start":
        for course in payload["courses"]:
            control.check()
            emit({"type": "course", "key": course_key(course), "status": "running"})
            for value in range(0, 101, 5):
                sleep(0.18)
                control.check()
                emit({"type": "progress", "value": value, "title": "示例视频 · 本地模拟"})
            emit({"type": "course", "key": course_key(course), "status": "done"})
            emit({"type": "log", "message": course["name"] + " · 演示任务已运行。", "level": "info"})
    else:
        sleep(0.5)
        control.check()
        emit({"type": "auth", "logged_in": True})
    message = ("演示操作完成，未连接平台或模型。" if action.startswith("assessment_")
               else "演示任务已结束，未执行任何真实学习操作。")
    emit({"type": "result", "message": message, "error": False})


class DemoProcess:
    """Thread implementation of the worker interface, isolated to demo mode."""

    pid = None

    def __init__(self, payload, data_dir, on_event, on_exit):
        self.payload = copy.deepcopy(payload)
        self.on_event, self.on_exit = on_event, on_exit
        self.reason = None
        self.exitcode = None
        self.thread = None
        self.terminated = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="KillZHS-demo", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        control = TaskControl(lambda: self.reason, self.payload.get("deadline"))
        code = 0

        def emit(event):
            if not self.terminated.is_set():
                self.on_event(event)

        try:
            run_demo(self.payload, emit, control)
        except TaskStopped as stopped:
            emit({"type": "result", "status": stopped.reason, "error": False,
                  "message": "演示任务已停止并完成收尾，未连接真实平台。"})
        except Exception:
            code = 1
            emit({"type": "result", "error": True, "message": "演示任务出错，请重新开始。"})
        finally:
            self.exitcode = code
            self.on_exit(code)

    def request_stop(self, reason="stopped"):
        self.reason = reason

    def confirm_verification(self, probe_id=None):
        pass

    def terminate(self):
        self.reason = "stopped"
        self.terminated.set()

    def kill(self):
        self.terminate()

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def join(self, timeout=None):
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout)
