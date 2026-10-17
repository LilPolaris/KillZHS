"""Start the source or frozen app and verify loopback-only demo/worker flows."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def request(url, name=None, *, token=None, params=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Local-Token"] = token
    data = json.dumps(params or {}).encode() if name else None
    target = url + ("/api/actions/" + name if name else "/api/state")
    with urlopen(Request(target, data=data, headers=headers), timeout=3) as response:
        return json.load(response)


def wait_for(url, predicate, *, timeout=30, process=None):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Application exited before HTTP check: {process.returncode}")
        try:
            last = request(url)
            if predicate(last):
                return last
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise TimeoutError(f"Local application did not reach expected state: {last and last.get('status')}")


def verify(command, output):
    checks = []
    with tempfile.TemporaryDirectory(prefix="killzhs-acceptance-") as temporary:
        for demo in (True, False):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            args = [*command, "--headless", "--no-browser", "--port", str(port), "--data-dir", str(Path(temporary) / str(demo))]
            if demo:
                args.append("--demo")
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
            process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            token = None
            try:
                state = wait_for(url, lambda row: row.get("app") == "KillZHS", process=process)
                token = state["token"]
                assert state["demo"] is demo
                if demo:
                    keys = [f"{row['kind']}:{row['id']}" for row in state["courses"]]
                    request(url, "start", token=token, params={"courses": keys})
                    wait_for(url, lambda row: row["status"] == "running", process=process)
                    started = time.monotonic()
                    request(url, "stop", token=token)
                    stopped = wait_for(url, lambda row: row["status"] == "stopped", process=process)
                    assert stopped.get("history") and stopped["history"][0]["has_log"]
                    assert time.monotonic() - started < 10
                    checks.append({"check": "demo_start_stop_history", "passed": True})
                else:
                    assert not state["saved_session"]
                    request(url, "diagnose", token=token)
                    diagnosed = wait_for(url, lambda row: row["status"] != "running", process=process)
                    assert diagnosed["status"] == "done", diagnosed["message"]
                    assert "检查通过" in diagnosed["message"]
                    assert diagnosed["history"][0]["action"] == "diagnose"
                    checks.append({"check": "spawned_dependency_diagnose", "passed": True})
                request(url, "quit", token=token)
                assert process.wait(timeout=10) == 0
                checks.append({"check": "clean_shutdown_demo" if demo else "clean_shutdown_worker", "passed": True})
            finally:
                if process.poll() is None:
                    if token:
                        try:
                            request(url, "quit", token=token)
                            process.wait(timeout=10)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
    report = {"command": command, "checks": checks, "passed": all(row["passed"] for row in checks)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="An existing frozen executable; source is the default")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "acceptance" / "source.json")
    args = parser.parse_args()
    command = [str(args.exe.resolve())] if args.exe else [sys.executable, "-B", "-m", "killzhs"]
    print(json.dumps(verify(command, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
