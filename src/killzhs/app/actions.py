"""Validated page actions. Registry metadata declares which actions can run live."""

import copy
from dataclasses import dataclass

from ..models import (
    AIConfig,
    Course,
    InputError,
    NotificationConfig,
    Plan,
    parse_plan,
    parse_settings,
    update_ai,
    update_notifications,
)


@dataclass(frozen=True)
class Action:
    handler: object
    while_running: bool = False


REGISTRY = {}


def register(name, *, while_running=False):
    def decorate(handler):
        if name in REGISTRY:
            raise ValueError(f"Duplicate action: {name}")
        REGISTRY[name] = Action(handler, while_running)
        return handler
    return decorate


def dispatch(controller, data, *, scheduled=False, budget=None):
    if not isinstance(data, dict):
        raise InputError("请求必须为 JSON 对象。")
    name = data.get("action")
    entry = REGISTRY.get(name) if isinstance(name, str) else None
    if entry is None:
        raise InputError("不支持的操作。")
    # Stopping may wait for a process; do not hold the controller lock here.
    if name == "stop":
        return entry.handler(controller, data, scheduled=scheduled, budget=budget)
    with controller.lock:
        if controller.closed.is_set():
            raise InputError("程序正在退出，请重新打开工作台。")
        if not entry.while_running and controller.status == "running":
            raise InputError("已有任务正在运行，请等待结束或先停止。")
        return entry.handler(controller, data, scheduled=scheduled, budget=budget)


@register("stop", while_running=True)
def stop(controller, _data, **_):
    controller.stop()


@register("confirm_verification", while_running=True)
def confirm_verification(controller, data, **_):
    probe = controller.verification_probe
    if (controller.status != "running" or controller.stop_requested or not controller.verify_probe_enabled
            or not probe or not probe.get("awaiting_confirmation") or not controller.process
            or data.get("task_id") != controller.task_id or data.get("probe_id") != probe.get("probe_id")):
        raise InputError("当前没有可确认的验证实验，页面可能已过期，请刷新后重试。")
    if probe.get("confirmation_sent"):
        raise InputError("确认已发送，请等待本次请求结果，不要重复点击。")
    controller.process.confirm_verification(probe["probe_id"])
    probe["confirmation_sent"] = True
    controller.log("验证实验：已收到用户的网页验证完成确认，等待 worker 重发一次原请求。")


@register("ai_settings", while_running=True)
def ai_settings(controller, data, **_):
    updated = update_ai(AIConfig.model_validate(controller.ai), data.get("ai"))
    controller.save_preference("ai", updated)
    controller.log("AI 设置已保存，将在下次任务生效，没有发送模型请求。")


@register("notification_settings", while_running=True)
def notification_settings(controller, data, **_):
    updated = update_notifications(NotificationConfig.model_validate(controller.notifications), data.get("notifications"))
    controller.save_preference("notifications", updated)
    controller.log("通知设置已保存。保存不会发送消息。")


@register("test_notification", while_running=True)
def test_notification(controller, _data, **_):
    config = update_notifications(NotificationConfig.model_validate(controller.notifications), {"enabled": True})
    if controller.demo:
        controller.log("演示模式：测试通知已模拟，未连接推送服务。")
    else:
        controller.notifier.submit(config, "test", "KillZHS 测试通知",
                                   "通知渠道已连接。任务提醒仅包含运行状态，不包含密钥或课程内容。", test=True)


@register("plan", while_running=True)
def plan(controller, data, **_):
    updated = parse_plan(data.get("plan"), {course_key(c) for c in controller.courses})
    if updated.enabled and not controller.settings["remember"]:
        raise InputError("定时任务需要记住登录状态，请先在偏好设置中开启。")
    controller.save_preference("plan", updated)
    controller.log("定时计划已保存，当前任务不受影响。" if updated.enabled else "未来定时计划已暂停，当前任务不受影响。")


@register("pause_plan", while_running=True)
def pause_plan(controller, _data, **_):
    # Disabling must still work after a sync removed a formerly selected course.
    updated = Plan.model_construct(**{**controller.plan, "enabled": False})
    controller.save_preference("plan", updated)
    controller.log("未来定时计划已暂停，当前任务不受影响。")


@register("settings")
def settings(controller, data, **_):
    updated = parse_settings(data.get("settings"))
    if not updated.remember and controller.plan["enabled"]:
        raise InputError("请先暂停定时计划，再关闭记住登录状态。")
    controller.save_preference("settings", updated)
    if not updated.remember:
        controller.store.delete("cookies.json")
    controller.log("运行设置已保存。")


def normalize_course(row):
    return {**Course.parse(row).to_dict(), "status": "ready"}


def course_key(course):
    return course["kind"] + ":" + course["id"] + (":" + course["class_id"] if course["kind"] == "ai" else "")


@register("add")
def add(controller, data, **_):
    course = normalize_course(data.get("course"))
    if course_key(course) in {course_key(c) for c in controller.courses}:
        raise InputError("这门课程已经在清单中了。")
    controller.courses.append(course)
    controller.persist_courses()


def item_key(row):
    return ":".join((row["course_key"], row["kind"], row["id"], row["exam_id"]))


def validate_selection(rows, keys, course):
    if not isinstance(keys, list) or not keys or len(keys) > 50 or any(not isinstance(k, str) for k in keys):
        raise InputError("请选择 1–50 份测验或试卷。")
    known = {item_key(row): row for row in rows if row.get("course_key") == course}
    if any(key not in known for key in keys):
        raise InputError("测验清单已变化，请重新同步并选择。")
    selected = [copy.deepcopy(known[key]) for key in dict.fromkeys(keys)]
    if any(row.get("state") not in (1, 2, 5) for row in selected):
        raise InputError("所选测验已提交或状态不支持，请重新同步。")
    return selected


def start_job(controller, data, *, scheduled=False, budget=None):
    action = data["action"]
    if controller.dependencies and not controller.demo:
        raise InputError("当前 Python 环境缺少依赖：" + "、".join(controller.dependencies) + "。请用项目虚拟环境启动。")
    selected, assessments = [], []
    if action in ("start", "assessment_scan", "assessment_run"):
        if scheduled and controller.verification:
            raise InputError("上次任务仍需手动验证，请在工作台打开课程验证后重新开始。")
        if not controller.logged_in and not controller.has_session() and not controller.demo:
            raise InputError("请先登录并同步课程。")
        keys = data.get("courses", [])
        if not isinstance(keys, list) or not keys or len(keys) > 200:
            raise InputError("请至少选择一门课程。")
        known = {course_key(c): c for c in controller.courses}
        if any(not isinstance(key, str) or key not in known for key in keys):
            raise InputError("课程清单已变化，请重新选择。")
        selected = [copy.deepcopy(known[key]) for key in dict.fromkeys(keys)]
        if action.startswith("assessment_") and (len(selected) != 1 or selected[0]["kind"] != "zhidao"):
            raise InputError("章节测验与考试目前请选择一门共享课。")
        ai = AIConfig.model_validate(controller.ai)
        if action == "assessment_run":
            assessments = validate_selection(controller.assessments, data.get("assessments"), course_key(selected[0]))
            if not ai.enabled:
                raise InputError("请先在 AI 配置中启用 AI。")
            if not ai.use_zhidao_ai and not ai.custom_ready:
                raise InputError("请先保存模型密钥和准确的模型名称。")
        if any(c["kind"] == "ai" for c in selected) and not ai.no_exam:
            if not ai.enabled:
                raise InputError("AI 答题尚未启用。请开启 AI，或选择跳过测验。")
            if not ai.use_zhidao_ai and not ai.custom_ready:
                raise InputError("请填写自定义 AI 的密钥和模型名称，或选择跳过测验。")
    controller.begin_job(action, selected, assessments, scheduled=scheduled, budget=budget)


for _name in ("login", "refresh", "start", "test_ai", "diagnose", "assessment_scan", "assessment_run"):
    register(_name)(start_job)
