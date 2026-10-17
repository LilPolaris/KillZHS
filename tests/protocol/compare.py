"""Golden trace format and comparison, shared by the legacy recorder and protocol tests."""

import json
from collections import Counter

import requests

from .fake_platform import background_view

HEARTBEAT = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/updateUserUsedTime"
RECORDED_REQUESTS_ENCODING = "gzip, deflate, zstd"


def background_summary(entries):
    """Background order and heartbeat counts depend on thread timing; keep what is stable."""
    views = [json.dumps(background_view(entry), ensure_ascii=False, sort_keys=True) for entry in entries]
    regular = Counter(view for view, entry in zip(views, entries, strict=True) if entry["url"] != HEARTBEAT)
    heartbeat = sorted({view for view, entry in zip(views, entries, strict=True) if entry["url"] == HEARTBEAT})
    return {"counts": dict(sorted(regular.items())), "heartbeat": heartbeat}


def trace(platform, *, seed, outcome, results, logs):
    return {"seed": seed, "outcome": outcome, "results": results, "logs": logs,
            "unhandled": platform.unhandled, "requests": platform.requests,
            "background": background_summary(platform.background)}


def request_differences(expected, actual, limit=3):
    """Compare wire data, adapting only requests' interpreter-dependent default.

    The recorder used Python 3.14, whose stdlib enables zstd. Python 3.12's
    requests default advertises only decoders available there. Explicit platform
    headers (gzip, deflate, br) and every other header/payload remain strict.
    Golden files are never rewritten and advertised unsupported decoders still
    fail against the current environment's actual requests default.
    """
    problems = []
    for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
        if left.get("headers", {}).get("accept-encoding") == RECORDED_REQUESTS_ENCODING:
            left = {**left, "headers": {**left["headers"],
                                       "accept-encoding": requests.utils.DEFAULT_ACCEPT_ENCODING}}
        if left != right:
            fields = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
            problems.append(f"request #{index} {left.get('url')} differs in {fields}:\n"
                            f"  expected {json.dumps({k: left.get(k) for k in fields}, ensure_ascii=False)}\n"
                            f"  actual   {json.dumps({k: right.get(k) for k in fields}, ensure_ascii=False)}")
            if len(problems) >= limit:
                break
    if len(expected) != len(actual):
        problems.append(f"expected {len(expected)} requests, got {len(actual)}")
    return problems
