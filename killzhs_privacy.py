"""Redaction shared by the worker, task archive and notification diagnostics."""

import re


def collect_secrets(value, key=""):
    result = set()
    if isinstance(value, dict):
        for name, child in value.items():
            result.update(collect_secrets(child, str(name)))
    elif isinstance(value, list):
        for child in value:
            result.update(collect_secrets(child, key))
    elif isinstance(value, str) and len(value) >= 4 and key.lower() in (
            "value", "password", "token", "api_key", "username", "uuid", "pushplus_token", "bark_key"):
        result.add(value)
    return result


def redact(value, secrets=()):
    text = str(value)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"https?://[^\s'\"]+", lambda m: m[0].split("?")[0] + ("?[已隐藏]" if "?" in m[0] else ""), text)
    text = re.sub(r"(?i)(cookie|token|password|api[_-]?key|oncePassword|pwd)(\s*[:=]\s*)[^\s,;}]+", r"\1\2[已隐藏]", text)
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)[:1500]
