"""The only messages allowed from a job into the application."""

from typing import Any, TypedDict

EVENT_TYPES = frozenset({
    "log", "result", "progress", "course", "courses", "cookies", "auth", "qr",
    "ai_usage", "assessments", "assessment_course", "assessment_result", "verification_probe",
})


class Event(TypedDict):
    type: str


def validate_event(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in EVENT_TYPES
