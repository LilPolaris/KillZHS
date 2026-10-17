"""Secret collection and redaction for logs, task archives and notification reports."""

import re

SECRET_KEYS = {"value", "password", "token", "api_key", "username", "uuid", "pushplus_token", "bark_key",
               "device_key", "cookie", "authorization", "access_token", "refresh_token", "oncepassword", "pwd"}
HIDDEN = "[已隐藏]"


def collect_secrets(value, key=""):
    """Strings stored under credential-like keys anywhere in a nested structure."""
    if isinstance(value, dict):
        return set().union(*(collect_secrets(child, str(name)) for name, child in value.items())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(collect_secrets(child, key) for child in value)) if value else set()
    if isinstance(value, str) and value and key.lower().replace("-", "_") in SECRET_KEYS:
        return {value}
    return set()


def redact(value, secrets=()):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(value))
    for secret in sorted((item for item in secrets if isinstance(item, str) and item), key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    text = re.sub(r"https?://[^\s'\"]+", _redact_url, text)
    text = re.sub(r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;}]+", r"\1" + HIDDEN, text)
    names = r"cookie|token|password|api[_-]?key|oncePassword|pwd|pushplus_token|bark_key|device_key|access_token|refresh_token|authorization"
    text = re.sub(rf"(?i)(\b(?:{names})\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}}]+)",
                  lambda match: match[1] + HIDDEN, text)
    return text[:1500]


def _redact_url(match):
    url = match[0]
    url = re.sub(r"(https?://)[^/@]+@", r"\1" + HIDDEN + "@", url)
    return re.split(r"[?#]", url, maxsplit=1)[0] + ("?" + HIDDEN if "?" in url or "#" in url else "")
