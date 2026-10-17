"""Job execution with explicit clients, progress callbacks and generation budgets."""

import base64
import importlib
import logging
import os
import re
import time

from ..models import AIConfig, Course, RunSettings
from ..privacy import collect_secrets, redact
from ..runtime import TaskControl, TaskStopped


class LogBridge(logging.Handler):
    def __init__(self, log, issues):
        super().__init__()
        self.log, self.issues = log, issues

    def emit(self, record):
        if record.levelno >= logging.ERROR and not getattr(record, "diagnostic", False):
            self.issues.add("attention")
        level = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
        self.log(record.getMessage(), level)


def execute(payload, data_dir, *, emit, stop_signal=None, confirmed=lambda: None):
    """Always emit a structured result, including cooperative stops and failures."""
    from ..ai.providers import AIBudgetExceeded

    secrets = collect_secrets(payload)
    issues = set()

    def log(message, level="info"):
        emit("log", message=redact(message, secrets), level=level)

    logger = logging.getLogger("killzhs")
    old_level, old_propagate = logger.level, logger.propagate
    bridge = LogBridge(log, issues)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(bridge)
    try:
        _run(payload, data_dir, emit, log, secrets, issues, stop_signal, confirmed)
        return 0
    except AIBudgetExceeded:
        log("已达到本次 AI 请求上限，任务停止。", "warning")
        emit("result", message="已达到 AI 请求上限，请查看任务记录。", error=True)
        return 1
    except TaskStopped as stopped:
        status = "limited" if stopped.reason == "limited" else "stopped"
        message = ("已达到本次总时长" if status == "limited" else "已按请求停止任务") + "，已尝试完成收尾；进度以平台实际接收为准。"
        reason = "verification" if "verification" in issues else None
        if reason:
            message += " 收尾时需要手动验证，请先到知到官方页面处理后再开始任务。"
        log(message, "warning")
        emit("result", message=message, status=status, error=False, reason=reason)
        return 0
    except Exception as error:
        log(error, "error")
        emit("result", message="任务未能完成，请查看运行日志。", error=True)
        return 1
    finally:
        logger.removeHandler(bridge)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _run(payload, data_dir, emit, log, secrets, issues, stop_signal, confirmed):
    from ..ai.providers import RequestBudget
    from ..platform.ai_course import AICourse
    from ..platform.auth import Auth
    from ..platform.hike import HikeCourses
    from ..platform.http import Http, cookies_to_list
    from ..platform.shared import SharedCourses
    from ..platform.verification import VerificationProbe

    action = payload["action"]
    settings = RunSettings.model_validate(payload.get("settings", {})).model_dump()
    ai = AIConfig.model_validate(payload.get("ai", {})).model_dump()
    control = TaskControl(stop_signal, payload.get("deadline"))
    control.check()
    if action == "diagnose":
        # Imports above exercise every course module in the actual child process.
        from ..platform.assessments import SharedAssessments  # noqa: F401
        for module in ("requests", "Crypto", "openai", "tiktoken", "pydantic"):
            importlib.import_module(module)
        emit("result", message="运行环境检查通过，Python 依赖与课程模块均可加载。", error=False)
        return
    if action == "test_ai":
        import requests

        if ai["use_zhidao_ai"]:
            raise ValueError("平台 AI 使用登录会话，不支持独立模型列表检查。")
        provider = ai["openai"]
        if not provider["api_key"]:
            raise ValueError("请先保存自定义服务的 API Key。")
        with requests.Session() as session:
            response = session.get(provider["api_base"].rstrip("/") + "/models",
                                   headers={"Authorization": "Bearer " + provider["api_key"]}, timeout=15)
            response.raise_for_status()
            models = response.json().get("data", [])
        available = any(isinstance(model, dict) and model.get("id") == provider["model_name"] for model in models)
        message = "模型服务可访问，已找到所填模型。" if available else "模型服务可访问，列表未包含所填模型；请与服务商核对模型名称。"
        log(message, "info" if available else "warning")
        emit("result", message=message, error=False)
        return

    http = Http(control=control, issues=issues)
    http.secrets = secrets
    cookies = payload.get("cookies", [])
    if cookies:
        try:
            http.cookies = cookies
            if http.uuid:
                secrets.add(str(http.uuid))
        except (ValueError, KeyError, TypeError):
            cookies = []
    budget = RequestBudget(ai["max_requests"], on_use=lambda count: emit("ai_usage", count=count))
    last_progress = [0.0]

    def progress(current, total, title, waiting=False):
        now = time.monotonic()
        if now - last_progress[0] >= .5 or current >= total:
            last_progress[0] = now
            emit("progress", value=round(max(0, min(100, current / total * 100)), 1) if total else 0,
                 title="短暂暂停，等待继续" if waiting else redact(title, secrets) or "当前视频",
                 phase="waiting" if waiting else "video", remaining=max(0, round(total - current)) if waiting else None)

    options = {"speed": settings["speed"], "threshold": settings["threshold"], "progress": progress}
    shared, hike = SharedCourses(http, control, **options), HikeCourses(http, control, **options)
    ai_course = AICourse(http, control, ai_config=ai, data_dir=data_dir, budget=budget, **options)
    try:
        if action == "login":
            log("正在生成登录二维码…")
            http.cookies = {}
            Auth(http).login(qr_callback=lambda image: emit(
                "qr", image="data:image/png;base64," + base64.b64encode(image).decode("ascii")))
            secrets.update(collect_secrets(cookies_to_list(http.cookies)))
            if http.uuid:
                secrets.add(str(http.uuid))
            emit("cookies", cookies=cookies_to_list(http.cookies))
            emit("auth", logged_in=True)
            log("登录成功，正在同步课程。")
        elif not cookies:
            emit("auth", logged_in=False)
            raise ValueError("没有可用的登录状态，请先扫码登录。")
        if action in ("login", "refresh"):
            _sync(http, shared, hike, ai_course, emit, log)
            return
        if action in ("assessment_scan", "assessment_run"):
            _assessments(payload, http, control, ai, data_dir, budget, emit, log)
            return
        if action != "start":
            raise ValueError("未知任务。")
        http.batch_started = time.monotonic() - max(0, time.time() - payload.get("started_at", time.time()))

        def probe_report(**values):
            if values["phase"] == "waiting":
                issues.add("verification")
            elif values["phase"] == "observing" and values.get("normal_reports") == 0:
                issues.discard("verification")
            emit("verification_probe", worker_pid=os.getpid(), **values)

        probe = VerificationProbe(http, shared, control, confirmed, payload.get("task_id"),
                                  enabled=payload.get("verify_probe_enabled") is True and os.environ.get("KILLZHS_VERIFY_PROBE") == "1",
                                  report=probe_report, log=log)
        http.probe = probe
        _courses(payload, http, shared, hike, ai_course, probe, control, issues, emit, log)
    finally:
        http.session.close()


def _sync(http, shared, hike, ai_course, emit, log):
    from ..platform.http import cookies_to_list

    rows, kinds, incomplete, failures = [], [], [], []
    for kind, client, id_field in (("zhidao", shared, "secret"), ("hike", hike, "courseId"), ("ai", ai_course, "courseId")):
        try:
            result = client.list()
            if not isinstance(result, list):
                raise ValueError("课程接口返回了无法识别的结构，可稍后重试或手动添加课程。")
            for row in result:
                try:
                    course = {"id": str(row[id_field]), "name": row.get("courseName") or row.get("name") or "未命名课程", "kind": kind}
                    if kind == "ai":
                        course["class_id"] = str(row["classId"])
                    rows.append(Course.parse(course).to_dict())
                except (KeyError, TypeError, ValueError):
                    if kind not in incomplete:
                        incomplete.append(kind)
                    log("已跳过一条不完整的课程数据，其余课程继续同步。", "warning")
            kinds.append(kind)
        except Exception as error:
            failures.append(kind)
            log({"zhidao": "共享课", "hike": "校内课", "ai": "AI 课程"}[kind] + "同步失败：" + str(error), "warning")
    if not kinds:
        emit("auth", logged_in=False)
        raise ValueError("课程同步失败，请检查网络或重新扫码登录。")
    emit("auth", logged_in=True)
    emit("cookies", cookies=cookies_to_list(http.cookies))
    emit("courses", courses=rows, kinds=kinds, incomplete_kinds=incomplete)
    partial = bool(failures or incomplete)
    message = f"已同步 {len(rows)} 门课程。" + ("部分数据同步不完整，已保留原有课程，请查看日志。" if partial else "")
    log(message, "warning" if partial else "info")
    emit("result", message=message, error=partial)


def _courses(payload, http, shared, hike, ai_course, probe, control, issues, emit, log):
    from ..platform.http import CaptchaRequired

    attention = 0
    for row in payload["courses"]:
        course = Course.parse(row)
        kind = course.kind
        if kind == "auto":
            kind = "zhidao" if re.search("[a-zA-Z]", course.id) else "hike"
        probe.course_kind = kind
        http.video = None
        control.begin_course(payload["settings"]["limit"])
        control.check()
        issues.clear()
        emit("course", key=course.key, status="running", message="正在运行 · " + course.name)
        log("开始课程：" + course.name)
        try:
            if kind == "ai":
                ai_course.run(int(course.id), int(course.class_id))
            else:
                {"zhidao": shared, "hike": hike}[kind].run(course.id)
            status = "attention" if "attention" in issues else "done"
        except TaskStopped as stopped:
            if stopped.reason != "course_limit":
                raise
            status = "limited"
            log("已达到本门课程的实际运行时限，已尝试保存最后进度。", "warning")
        except Exception as error:
            status = "attention"
            if isinstance(error, CaptchaRequired):
                issues.add("verification")
            log("课程执行失败：" + str(error), "error")
        finally:
            probe.finish("course_ended")
        attention += status == "attention"
        emit("course", key=course.key, status=status)
        log(course.name + " · " + {"done": "本次任务已运行。", "limited": "已达到本次时限。", "attention": "存在异常，请查看日志。"}[status])
        if "verification" in issues:
            emit("result", message="需要手动验证，本批次已停止。请先打开知到官方页面完成验证，再手动开始任务。", error=True, reason="verification")
            return
    message = f"本次任务已结束，{attention} 门课程需要查看日志。" if attention else "本次任务已结束。课程完成情况请以平台为准。"
    emit("result", message=message, error=bool(attention))


def _assessments(payload, http, control, ai, data_dir, budget, emit, log):
    from ..platform.assessments import AssessmentError, SharedAssessments, item_key

    course = Course.parse(payload["courses"][0])
    emit("assessment_course", key=course.key)
    service = SharedAssessments(http, control, ai, data_dir, budget=budget, emit=emit, log=log)
    active = None
    try:
        service.connect(course.to_dict())
        items, incomplete = service.scan()
        emit("assessments", course_key=course.key, items=items)
        if payload["action"] == "assessment_scan":
            emit("result", message=f"已同步 {len(items)} 份测验与试卷。" + ("部分列表未能读取，请查看日志。" if incomplete else ""), error=bool(incomplete))
            return
        current = {item_key(row): row for row in items}
        for requested in payload["assessments"]:
            active = item_key(requested)
            row = current.get(active)
            if row is None or row["state"] not in (1, 2, 5):
                raise AssessmentError("试卷状态已变化或已提交，请重新同步，不会继续自动答题。")
            log("开始自动答题并交卷：" + row["name"])
            emit("assessment_result", key=active, state="running", message="正在读取试卷并生成答案。")
            result = service.run(row)
            emit("assessment_result", **result)
            row["state"] = 4
            emit("assessments", course_key=course.key, items=items)
            log(row["name"] + " · " + result["message"])
        emit("result", message="所选试卷已由平台确认交卷，成绩以平台批阅为准。", error=False)
    except AssessmentError as error:
        if error.reason == "auth":
            emit("auth", logged_in=False)
        if active:
            emit("assessment_result", key=active, state="attention", message=str(error))
        emit("result", message=str(error), error=True, reason=error.reason)
    finally:
        if service.session:
            service.session.close()
