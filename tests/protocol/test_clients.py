"""Run the new clients against the same fixtures used to record the old client.

Only tests replace the transport/clock. Production sessions remain ordinary
explicit dependencies. Golden files are never rewritten by this test suite.
"""

import copy
import json
import random
import re
import threading
from pathlib import Path

import pytest
import requests

from killzhs.ai.providers import RequestBudget
from killzhs.platform.ai_course import AICourse
from killzhs.platform.assessments import SharedAssessments
from killzhs.platform.auth import Auth
from killzhs.platform.hike import HikeCourses
from killzhs.platform.http import Http
from killzhs.platform.shared import SharedCourses
from killzhs.runtime import TaskControl

from . import fake_platform as fake
from .compare import background_summary, request_differences
from .scenarios import SCENARIOS

GOLDEN = Path(__file__).with_name("golden")


@pytest.mark.parametrize("name", SCENARIOS)
def test_golden_protocol(name, monkeypatch, tmp_path, caplog):
    spec = SCENARIOS[name]
    clock = fake.Clock()
    transport = fake.Transport()
    # Register originals with pytest before the shared recording fixture installs.
    import time
    monkeypatch.setattr(requests.sessions.Session, "request", transport.original)
    for key in ("time", "sleep", "monotonic"):
        monkeypatch.setattr(time, key, getattr(time, key))
    transport.install(clock)
    platform = transport.platform = fake.Platform(**spec["platform"])
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(Path(__file__).parents[2] / ".local" / "tokenizer"))
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    previous_random = random.getstate()
    random.seed(spec["seed"])
    settings = spec["settings"]
    deadline = clock.time() + spec["deadline"] if spec["deadline"] else None
    control = TaskControl(deadline=deadline)
    http = Http(control=control)
    if spec["cookies"]:
        http.cookies = copy.deepcopy(spec["cookies"])
    budget = RequestBudget(spec["ai"]["max_requests"])
    options = {"speed": settings["speed"], "threshold": settings["threshold"]}
    shared, hike = SharedCourses(http, control, **options), HikeCourses(http, control, **options)
    ai = AICourse(http, control, ai_config=spec["ai"], data_dir=tmp_path, budget=budget, **options)
    results, outcome, logs = [], {"raised": None}, []
    threads = set(threading.enumerate())
    try:
        for op in spec["ops"]:
            if op[0] == "login":
                images = []
                http.cookies = {}
                Auth(http).login(images.append)
                results.append({"uuid": http.uuid, "cookies": sorted(c.name for c in http.cookies),
                                "qr_images": [image.decode() for image in images]})
            elif op[0] == "list":
                results.append({"zhidao": shared, "hike": hike, "ai": ai}[op[1]].list())
            elif op[0] == "run":
                control.begin_course(settings["limit"])
                control.check()
                {"zhidao": shared, "hike": hike, "ai": ai}[op[1]].run(*op[2:])
                results.append(None)
            elif op[0] == "assessments":
                events = []
                service = SharedAssessments(http, control, spec["ai"], tmp_path, budget=budget,
                    emit=lambda kind, events=events, **value: events.append([kind, value]),
                    log=lambda message, level="info": logs.append([level, message]))
                try:
                    service.connect({"id": op[1], "kind": "zhidao", "name": "示例共享课"})
                    items, incomplete = service.scan()
                    done = []
                    for wanted in op[2]:
                        kind, identifier = wanted.split(":")
                        done.append(service.run(next(r for r in items if r["kind"] == kind and r["id"] == identifier)))
                    results.append({"items": items, "incomplete": incomplete, "results": done, "events": events})
                finally:
                    if service.session:
                        service.session.close()
    except BaseException as error:
        outcome = {"raised": type(error).__name__, "reason": getattr(error, "reason", None), "message": str(error)}
    finally:
        for thread in set(threading.enumerate()) - threads:
            thread.join(timeout=2)
            assert not thread.is_alive(), f"Leaked background thread: {thread.name}"
        random.setstate(previous_random)
        http.session.close()
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    assert platform.unhandled == expected["unhandled"] == []
    assert outcome == expected["outcome"]
    assert not request_differences(expected["requests"], platform.requests), "\n".join(
        request_differences(expected["requests"], platform.requests))
    assert background_summary(platform.background) == expected["background"]
    assert results == expected["results"]
    logs = [[record.levelname.lower(), re.sub(r'"worker_pid": \d+', '"worker_pid": "<pid>"', record.getMessage())]
            for record in caplog.records if record.levelno >= 30] + logs
    assert logs == expected["logs"]
