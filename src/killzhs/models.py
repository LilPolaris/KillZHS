"""Validated preferences and courses. Every value from the browser or disk passes here.

Secret fields are stored but never leave the process: ``public_*`` helpers replace
them with ``has_*`` flags before anything reaches the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)


class InputError(ValueError):
    """A message meant for the person using the page."""


# ---- validation plumbing --------------------------------------------------

STRICT = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
RANGE_ERRORS = {"greater_than_equal", "less_than_equal", "greater_than", "less_than"}


def parse(model, data, *, rules, default, context=None):
    """Validate ``data``; turn the first problem into a single readable message.

    ``rules`` maps a location prefix to a message, or to a callable taking the
    pydantic error type. The longest matching prefix wins. A validator that
    raises :class:`InputError` supplies its own message.
    """
    if not isinstance(data, dict):
        raise InputError(default)
    try:
        return model.model_validate(data, context=context)
    except ValidationError as error:
        raise InputError(explain(error, rules, default)) from None


def explain(error, rules, default):
    item = error.errors()[0]
    cause = (item.get("ctx") or {}).get("error")
    if isinstance(cause, InputError):
        return str(cause)
    location = tuple(str(part) for part in item["loc"])
    matches = [(prefix, message) for prefix, message in rules if location[:len(prefix)] == prefix]
    if not matches:
        return default
    message = max(matches, key=lambda match: len(match[0]))[1]
    return message(item["type"]) if callable(message) else message


def merge(base, patch):
    """Apply a partial update: nested objects merge, everything else replaces."""
    result = dict(base)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def text(limit):
    def check(value):
        if len(value) > limit or any(ord(character) < 32 for character in value):
            raise ValueError("invalid text")
        return value.strip()
    return AfterValidator(check)


def http_url(value, *, message, add_version=False):
    if not isinstance(value, str) or len(value) > 500 or any(ord(character) < 32 for character in value):
        raise InputError(message)
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise InputError(message) from error
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        raise InputError(message)
    return value if parsed.path or not add_version else value + "/v1"


def api_url(value):
    return http_url(value, add_version=True,
                    message="API 地址需以 http:// 或 https:// 开头，不要在地址中填写密钥或查询参数。")


ApiUrl = Annotated[str, AfterValidator(api_url)]
Secret = Annotated[str, text(500)]
Name = Annotated[str, text(160)]


# ---- run settings ---------------------------------------------------------

class RunSettings(BaseModel):
    model_config = STRICT
    speed: float | None = Field(None, ge=0.1, le=16)
    limit: int = Field(0, ge=0, le=1440)
    threshold: float = Field(0.91, ge=0.1, le=2)
    remember: bool = True
    budget_minutes: int = Field(0, ge=0, le=240)


def _number(kind):
    return "参数超出范围，请检查倍速、时限或完成阈值。" if kind in RANGE_ERRORS else "请输入有效的数字。"


SETTINGS_RULES = [((name,), _number) for name in ("speed", "limit", "threshold", "budget_minutes")] + [
    (("remember",), "记住登录状态必须为开关值。")]


def parse_settings(value):
    return parse(RunSettings, value, rules=SETTINGS_RULES, default="设置格式不正确。")


# ---- AI -------------------------------------------------------------------

class OpenAIProvider(BaseModel):
    model_config = STRICT
    api_base: ApiUrl = "https://api.openai.com/v1"
    api_key: Secret = ""
    model_name: Name = ""


class MoonShot(BaseModel):
    model_config = STRICT
    base_url: ApiUrl = "https://api.moonshot.cn/v1"
    api_key: Secret = ""
    delete_after_convert: bool = True


class PptProcessing(BaseModel):
    model_config = STRICT
    provide_to_ai: bool = False
    moonShot: MoonShot = Field(default_factory=MoonShot)  # noqa: N815 - stored key name


class AIConfig(BaseModel):
    model_config = STRICT
    enabled: bool = False
    use_zhidao_ai: bool = True
    use_stream: bool = True
    no_exam: bool = True
    max_requests: int = Field(10, ge=1, le=100)
    openai: OpenAIProvider = Field(default_factory=OpenAIProvider)
    ppt_processing: PptProcessing = Field(default_factory=PptProcessing)

    @property
    def custom_ready(self):
        return bool(self.openai.api_key and self.openai.model_name)


AI_RULES = [((name,), "AI 开关值不正确。") for name in ("enabled", "use_zhidao_ai", "use_stream", "no_exam")] + [
    (("max_requests",), "单次任务的 AI 请求上限需为 1–100。"),
    (("openai",), "模型服务配置格式不正确。"),
    (("openai", "api_key"), "模型名称或密钥格式不正确。"),
    (("openai", "model_name"), "模型名称或密钥格式不正确。"),
    (("ppt_processing",), "PPT 设置格式不正确。"),
    (("ppt_processing", "provide_to_ai"), "PPT 开关不正确。"),
    (("ppt_processing", "moonShot"), "模型服务配置格式不正确。"),
    (("ppt_processing", "moonShot", "api_key"), "模型名称或密钥格式不正确。"),
    (("ppt_processing", "moonShot", "delete_after_convert"), "文件清理开关不正确。"),
]


def update_ai(previous, patch):
    """Merge a partial update; an omitted ``api_key`` keeps the stored key."""
    if not isinstance(patch, dict):
        raise InputError("AI 设置格式不正确。")
    return parse(AIConfig, merge(previous.model_dump(), patch), rules=AI_RULES, default="AI 设置格式不正确。")


def public_ai(ai):
    value = ai.model_dump()
    for provider in (value["openai"], value["ppt_processing"]["moonShot"]):
        provider["has_api_key"] = bool(provider.pop("api_key"))
    return value


# ---- notifications --------------------------------------------------------

def bark_url(value):
    return http_url(value, message="Bark 服务器需为 http(s) 地址，请把设备密钥单独填写。")


class NotificationEvents(BaseModel):
    model_config = STRICT
    finished: bool = True
    verification: bool = True
    auth: bool = True
    skipped: bool = True


class NotificationConfig(BaseModel):
    model_config = STRICT
    enabled: bool = False
    provider: Literal["pushplus", "bark"] = "pushplus"
    pushplus_token: Secret = ""
    bark_server: Annotated[str, AfterValidator(bark_url)] = "https://api.day.app"
    bark_key: Secret = ""
    events: NotificationEvents = Field(default_factory=NotificationEvents)

    @model_validator(mode="after")
    def require_secret(self):
        if self.enabled and not (self.pushplus_token if self.provider == "pushplus" else self.bark_key):
            raise InputError("启用通知前，请先填写所选渠道的密钥。")
        return self


NOTIFICATION_RULES = [
    (("enabled",), "通知开关值不正确。"), (("provider",), "请选择 PushPlus 或 Bark。"),
    (("events",), "通知事件设置不正确。"), (("pushplus_token",), "通知地址或密钥格式不正确。"),
    (("bark_key",), "通知地址或密钥格式不正确。"), (("bark_server",), "通知地址或密钥格式不正确。"),
]


def update_notifications(previous, patch):
    if not isinstance(patch, dict):
        raise InputError("通知设置格式不正确。")
    return parse(NotificationConfig, merge(previous.model_dump(), patch), rules=NOTIFICATION_RULES,
                 default="通知设置格式不正确。")


def public_notifications(config):
    value = config.model_dump()
    for key in ("pushplus_token", "bark_key"):
        value["has_" + key] = bool(value.pop(key))
    return value


# ---- daily plan -----------------------------------------------------------

class Plan(BaseModel):
    model_config = STRICT
    enabled: bool = False
    time: str = Field("20:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)))
    minutes: int = Field(30, ge=1, le=240)
    courses: list[str] = Field(default_factory=list)
    catchup_minutes: int = Field(0, ge=0, le=720)

    @field_validator("weekdays")
    @classmethod
    def known_days(cls, days):
        if not days or any(day not in range(7) for day in days):
            raise ValueError("invalid weekdays")
        return sorted(set(days))

    @field_validator("courses")
    @classmethod
    def known_courses(cls, courses, info: ValidationInfo):
        known = (info.context or {}).get("known", set())
        if len(courses) > 200 or any(key not in known for key in courses):
            raise ValueError("unknown course")
        return list(dict.fromkeys(courses))

    @model_validator(mode="after")
    def require_courses(self):
        if self.enabled and not self.courses:
            raise InputError("启用计划前，请至少选择一门课程。")
        return self

    def due_today(self, now):
        hour, minute = map(int, self.time.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def next_run(self, last_date=None, now=None):
        """The next moment the scheduler would start, including a pending catch-up."""
        if not self.enabled:
            return None
        now = now or datetime.now().astimezone()
        for delta in range(8):
            candidate = self.due_today(now + timedelta(days=delta))
            if candidate.weekday() in self.weekdays and candidate.date().isoformat() != last_date:
                if candidate > now:
                    return candidate.isoformat()
                late = (now - candidate).total_seconds()
                if delta == 0 and (late < 60 or (self.catchup_minutes and late <= self.catchup_minutes * 60)):
                    return now.isoformat()
        return None


PLAN_RULES = [
    (("enabled",), "请输入有效的计划时间。"), (("time",), "请输入有效的计划时间。"),
    (("weekdays",), "请至少选择一个执行日。"), (("minutes",), "计划时长需为 1–240 分钟。"),
    (("catchup_minutes",), "补跑窗口需为 0–720 分钟；0 表示关闭。"),
    (("courses",), "计划中的课程已变化，请重新选择。"),
]


def parse_plan(value, known_keys):
    return parse(Plan, value, rules=PLAN_RULES, default="定时计划格式不正确。", context={"known": set(known_keys)})


# ---- courses --------------------------------------------------------------

COURSE_KINDS = ("auto", "hike", "zhidao", "ai")


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    kind: str
    class_id: str | None = None

    @property
    def key(self):
        return f"{self.kind}:{self.id}" + (f":{self.class_id}" if self.kind == "ai" else "")

    def to_dict(self):
        value = {"id": self.id, "name": self.name, "kind": self.kind}
        if self.kind == "ai":
            value["class_id"] = self.class_id
        return value

    @classmethod
    def parse(cls, row: Any):
        if not isinstance(row, dict):
            raise InputError("课程格式不正确。")
        raw_id = row.get("id", "")
        if type(raw_id) not in (str, int):
            raise InputError("课程 ID 不正确，请粘贴课程地址中的 courseId 或 recruitAndCourseId。")
        identifier = str(raw_id).strip()
        if not identifier or len(identifier) > 256 or not all(
                character.isascii() and (character.isalnum() or character in "_-=") for character in identifier):
            raise InputError("课程 ID 不正确，请粘贴课程地址中的 courseId 或 recruitAndCourseId。")
        kind = row.get("kind", "auto")
        if kind not in COURSE_KINDS:
            raise InputError("课程类型不正确。")
        class_id = None
        if kind == "ai":
            class_id = str(row.get("class_id", "")).strip()
            if not identifier.isdecimal() or not class_id.isascii() or not class_id.isdecimal() or len(class_id) > 30:
                raise InputError("AI 课程需要数字课程 ID 和班级 ID。")
        return cls(identifier, str(row.get("name") or f"课程 {identifier}")[:160], kind, class_id)


def unique_courses(rows):
    """Parse rows, keep the first of each key, and report how many rows were unusable."""
    courses, seen, skipped = [], set(), []
    for row in rows:
        try:
            course = Course.parse(row)
        except InputError:
            skipped.append(row)
            continue
        if course.key not in seen:
            seen.add(course.key)
            courses.append(course)
    return courses, skipped
