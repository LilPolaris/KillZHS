"""Record golden protocol traces by running the OLD project's client on the fake platform.

Run with the old project's interpreter; the old directory is only read:

    <old>/.venv/Scripts/python.exe -B scripts/record_legacy.py --legacy <old project>

Byte-code writing is disabled and every path the old code asks for is redirected
into a temporary directory. ``--check`` re-records into memory and compares with
the committed golden files, which shows whether the recording is deterministic.
The old worker's request hook is emulated: stop checks, AI request counting and
a 30-second default timeout apply to course and assessment runs only, exactly
where the old worker installed them.
"""

import argparse
import contextlib
import copy
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "protocol" / "golden"


def plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


class Legacy:
    def __init__(self, directory, work):
        self.work = work
        self.local = None
        sys.path.insert(0, str(directory))
        import utils
        original = utils.getRealPath

        def redirect(path):
            relative = str(path).replace("\\", "/").removeprefix("./")
            if relative == "logs":
                return str(work / "logs")
            if relative.split("/")[0] in ("aiexamAnswer", "AiDownloadCache") and self.local is not None:
                return str((self.local / relative).resolve())
            raise RuntimeError(f"legacy code asked for an unexpected path: {path} ({original})")

        utils.getRealPath = redirect
        import fucker
        import killzhs_ai_exam
        import killzhs_assessments
        import killzhs_runtime
        import killzhs_verification
        from logger import logger
        self.logs = []
        recorder = self

        class Capture(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    message = re.sub(r'"worker_pid": \d+', '"worker_pid": "<pid>"', record.getMessage())
                    recorder.logs.append([record.levelname.lower(), message])

        logger.setLevel("INFO")
        logger.addHandler(Capture())
        fucker.progressBar = killzhs_ai_exam.progressBar = lambda *_, **__: None
        self.fucker, self.assessments = fucker, killzhs_assessments
        self.runtime, self.verification = killzhs_runtime, killzhs_verification


def run_scenario(name, spec, legacy, transport, fake):
    clock = fake.Clock()
    transport.install(clock)
    platform = fake.Platform(**spec["platform"])
    transport.platform = platform
    random.seed(spec["seed"])
    legacy.local = legacy.work / name
    legacy.local.mkdir(parents=True)
    legacy.logs = []
    settings, ai = spec["settings"], copy.deepcopy(spec["ai"])
    client = legacy.fucker.Fucker(proxies={}, speed=settings["speed"], limit=int(settings["limit"]),
                                  end_thre=settings["threshold"], tree_view=True, progressbar_view=True)
    deadline = clock.time() + spec["deadline"] if spec["deadline"] else None
    control = legacy.runtime.TaskControl(None, deadline)
    client.task_control = control
    if spec["cookies"]:
        client.cookies = copy.deepcopy(spec["cookies"])
    budget = [0]

    def worker_hook(url, timeout):
        control.check()
        if "/chat/completions" in str(url) or "/qa/platform/stream" in str(url):
            if budget[0] >= ai["max_requests"]:
                raise RuntimeError("AI budget exceeded")
            budget[0] += 1
        return 30 if timeout is None else timeout

    results, outcome = [], {"raised": None}
    threads = set(threading.enumerate())
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for op in spec["ops"]:
                transport.before_request = None
                if op[0] == "login":
                    images = []
                    client.cookies = {}
                    client.login(use_qr=True, interactive=False, qr_callback=images.append)
                    results.append({"uuid": client.uuid, "cookies": sorted(c.name for c in client.cookies),
                                    "qr_images": [image.decode() for image in images]})
                elif op[0] == "list":
                    method = {"zhidao": client.getZhidaoList, "hike": client.getHikeList,
                              "ai": client.getZhidaoAiList}[op[1]]
                    results.append(plain(method()))
                elif op[0] == "run":
                    transport.before_request = worker_hook
                    client.diagnostic_secrets = set()
                    client.batch_started_monotonic = clock.monotonic()
                    probe = legacy.verification.VerificationProbe(client, control, None, "task", enabled=False)
                    client.verification_probe = probe
                    probe.course_kind = op[1]
                    client.verification_video = None
                    control.begin_course(settings["limit"])
                    control.check()
                    if op[1] == "zhidao":
                        client.fuckZhidaoCourse(op[2])
                    elif op[1] == "hike":
                        client.fuckHikeCourse(op[2])
                    else:
                        client.fuckAiCourse(int(op[2]), int(op[3]), aiConfig=ai, no_exam=ai["no_exam"])
                    results.append(None)
                elif op[0] == "assessments":
                    transport.before_request = worker_hook
                    events = []
                    course = {"id": op[1], "kind": "zhidao", "name": "示例共享课"}
                    service = legacy.assessments.SharedAssessments(
                        client, control, ai, legacy.local, emit=lambda kind, events=events, **value: events.append([kind, value]),
                        log=lambda message, level="info": legacy.logs.append([level, message]))
                    service.remaining_requests = lambda: ai["max_requests"] - budget[0]
                    try:
                        service.connect(course)
                        items, incomplete = service.scan()
                        done = []
                        for wanted in op[2]:
                            kind, identifier = wanted.split(":")
                            row = next(r for r in items if r["kind"] == kind and r["id"] == identifier)
                            done.append(service.run(row))
                    finally:
                        if service.session:
                            service.session.close()
                    results.append({"items": items, "incomplete": incomplete, "results": done, "events": events})
    except BaseException as error:  # TaskStopped derives from BaseException on purpose.
        outcome = {"raised": type(error).__name__, "reason": getattr(error, "reason", None), "message": str(error)}
    for thread in set(threading.enumerate()) - threads:
        thread.join(timeout=15)
        if thread.is_alive():
            outcome.setdefault("stuck_threads", []).append(thread.name)
    transport.before_request = None
    from tests.protocol.compare import trace
    return trace(platform, seed=spec["seed"], outcome=outcome, results=plain(results), logs=legacy.logs)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--legacy", required=True, type=Path, help="old fuckZHS project directory (read only)")
    parser.add_argument("--only", nargs="*", help="scenario names")
    parser.add_argument("--check", action="store_true", help="compare with committed golden files")
    parser.add_argument("--find-pause-seed", action="store_true")
    args = parser.parse_args()
    tokenizer = ROOT / ".local" / "tokenizer"
    if not tokenizer.is_dir():
        raise SystemExit("Copy a tiktoken cache to .local/tokenizer first (no download is attempted).")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(tokenizer)
    sys.path.insert(0, str(ROOT))
    from tests.protocol import fake_platform as fake
    from tests.protocol.compare import request_differences
    from tests.protocol.scenarios import SCENARIOS
    work = Path(tempfile.mkdtemp(prefix="killzhs-legacy-"))
    transport = fake.Transport()
    legacy = Legacy(args.legacy.resolve(), work)
    failures = 0
    try:
        if args.find_pause_seed:
            original = legacy.fucker.random
            for seed in range(100):
                low = []
                legacy.fucker.random = lambda low=low: low.append(value := original()) or value
                spec = {**SCENARIOS["shared_course"], "seed": seed}
                run_scenario(f"seed-{seed}", spec, legacy, transport, fake)
                if any(value < 0.0025 for value in low):
                    print("pause seed:", seed)
                    break
            legacy.fucker.random = original
            return 0
        for name, spec in SCENARIOS.items():
            if args.only and name not in args.only:
                continue
            result = run_scenario(name, spec, legacy, transport, fake)
            path = GOLDEN / f"{name}.json"
            if args.check:
                golden = json.loads(path.read_text(encoding="utf-8"))
                problems = request_differences(golden["requests"], result["requests"])
                problems += [f"{key} differs" for key in ("outcome", "results", "background", "logs", "unhandled")
                             if golden[key] != result[key]]
                failures += bool(problems)
                print(("FAIL " if problems else "ok   ") + name, *problems, sep="\n  ")
            else:
                GOLDEN.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                print(f"{name}: {len(result['requests'])} requests, {sum(result['background']['counts'].values())} "
                      f"background, outcome {result['outcome']['raised']}, unhandled {len(result['unhandled'])}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    sys.stdout.flush()
    # Old exam heartbeat threads are non-daemon; never let one keep the recorder alive.
    os._exit(1 if failures else 0)


if __name__ == "__main__":
    main()
