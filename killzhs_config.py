"""Validated local preferences. Secret fields never appear in browser snapshots."""

import copy
from datetime import datetime, timedelta
import re
from urllib.parse import urlsplit

VERSION = "0.1.0-beta.4"
DEFAULTS = {"speed": None, "limit": 0, "threshold": 0.91, "remember": True, "budget_minutes": 0}


class InputError(ValueError):
    pass


AI_DEFAULTS = {
    "enabled": False, "use_zhidao_ai": True, "use_stream": True,
    "no_exam": True, "max_requests": 10,
    "openai": {"api_base": "https://api.openai.com/v1", "api_key": "", "model_name": ""},
    "ppt_processing": {"provide_to_ai": False, "moonShot": {
        "base_url": "https://api.moonshot.cn/v1", "api_key": "", "delete_after_convert": True}},
}
PLAN_DEFAULTS = {"enabled": False, "time": "20:00", "weekdays": list(range(7)),
                 "minutes": 30, "courses": [], "catchup_minutes": 0}


def api_url(value):
    if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise InputError("API 地址格式不正确。")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise InputError("API 地址格式不正确。") from error
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InputError("API 地址需以 http:// 或 https:// 开头，不要在地址中填写密钥或查询参数。")
    try:
        parsed.port
    except ValueError as error:
        raise InputError("API 地址端口不正确。") from error
    return value if parsed.path else value + "/v1"


def validate_ai(value, previous=None):
    if not isinstance(value, dict) or set(value) - AI_DEFAULTS.keys():
        raise InputError("AI 设置格式不正确。")
    result = copy.deepcopy(previous or AI_DEFAULTS)
    for key in ("enabled", "use_zhidao_ai", "use_stream", "no_exam"):
        if key in value:
            if not isinstance(value[key], bool):
                raise InputError("AI 开关值不正确。")
            result[key] = value[key]
    if "max_requests" in value:
        number = value["max_requests"]
        if type(number) is not int or not 1 <= number <= 100:
            raise InputError("单次任务的 AI 请求上限需为 1–100。")
        result["max_requests"] = number

    def provider(incoming, saved, allowed):
        if not isinstance(incoming, dict) or set(incoming) - set(allowed):
            raise InputError("模型服务配置格式不正确。")
        for name, child in incoming.items():
            if name in ("api_base", "base_url"):
                saved[name] = api_url(child)
            elif name == "delete_after_convert":
                if not isinstance(child, bool):
                    raise InputError("文件清理开关不正确。")
                saved[name] = child
            elif not isinstance(child, str) or len(child) > (500 if name == "api_key" else 160) or any(ord(c) < 32 for c in child):
                raise InputError("模型名称或密钥格式不正确。")
            else:
                saved[name] = child.strip()

    if "openai" in value:
        provider(value["openai"], result["openai"], ("api_base", "api_key", "model_name"))
    if "ppt_processing" in value:
        ppt = value["ppt_processing"]
        if not isinstance(ppt, dict) or set(ppt) - {"provide_to_ai", "moonShot"}:
            raise InputError("PPT 设置格式不正确。")
        if "provide_to_ai" in ppt:
            if not isinstance(ppt["provide_to_ai"], bool):
                raise InputError("PPT 开关不正确。")
            result["ppt_processing"]["provide_to_ai"] = ppt["provide_to_ai"]
        if "moonShot" in ppt:
            provider(ppt["moonShot"], result["ppt_processing"]["moonShot"], ("base_url", "api_key", "delete_after_convert"))
    return result


def public_ai(value):
    result = copy.deepcopy(value)
    for provider in (result["openai"], result["ppt_processing"]["moonShot"]):
        provider["has_api_key"] = bool(provider.pop("api_key", ""))
    return result


def validate_plan(value, known_keys):
    if not isinstance(value, dict) or set(value) - PLAN_DEFAULTS.keys():
        raise InputError("定时计划格式不正确。")
    result = {**copy.deepcopy(PLAN_DEFAULTS), **value}
    if type(result["enabled"]) is not bool or not isinstance(result["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result["time"]):
        raise InputError("请输入有效的计划时间。")
    days = result["weekdays"]
    if not isinstance(days, list) or not days or any(type(day) is not int or day not in range(7) for day in days):
        raise InputError("请至少选择一个执行日。")
    result["weekdays"] = sorted(set(days))
    if type(result["minutes"]) is not int or not 1 <= result["minutes"] <= 240:
        raise InputError("计划时长需为 1–240 分钟。")
    if type(result["catchup_minutes"]) is not int or not 0 <= result["catchup_minutes"] <= 720:
        raise InputError("补跑窗口需为 0–720 分钟；0 表示关闭。")
    courses = result["courses"]
    if not isinstance(courses, list) or len(courses) > 200 or any(not isinstance(key, str) or key not in known_keys for key in courses):
        raise InputError("计划中的课程已变化，请重新选择。")
    if result["enabled"] and not courses:
        raise InputError("启用计划前，请至少选择一门课程。")
    result["courses"] = list(dict.fromkeys(courses))
    return result


def next_run(plan, last_date=None, now=None):
    if not plan["enabled"]:
        return None
    now = now or datetime.now().astimezone()
    hour, minute = map(int, plan["time"].split(":"))
    for delta in range(8):
        candidate = (now + timedelta(days=delta)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.weekday() in plan["weekdays"] and candidate.date().isoformat() != last_date:
            if candidate > now:
                return candidate.isoformat()
            late = (now - candidate).total_seconds()
            if delta == 0 and (late < 60 or (plan.get("catchup_minutes", 0) and late <= plan["catchup_minutes"] * 60)):
                return now.isoformat()
    return None
