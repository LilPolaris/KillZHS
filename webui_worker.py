"""One isolated job; communicate with the UI using structured stdout events."""

import base64
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import logging
from pathlib import Path
import re
import sys
import time

from webui import PREFIX, atomic_json, course_key, load_json

OUTPUT = sys.stdout
SECRETS = set()
ISSUES = set()


def redact(value):
    text = str(value)
    for secret in sorted(SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"https?://[^\s'\"]+", lambda m: m[0].split("?")[0] + ("?[已隐藏]" if "?" in m[0] else ""), text)
    text = re.sub(r"(?i)(cookie|token|password|api[_-]?key|oncePassword|pwd)(\s*[:=]\s*)[^\s,;}]+", r"\1\2[已隐藏]", text)
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)[:1500]


def remember_secrets(value, key=""):
    if isinstance(value, dict):
        for name, child in value.items():
            remember_secrets(child, str(name))
    elif isinstance(value, list):
        for child in value:
            remember_secrets(child, key)
    elif isinstance(value, str) and len(value) >= 4 and key.lower() in (
            "value", "password", "token", "api_key", "username", "uuid"):
        SECRETS.add(value)


def emit(event_type, **values):
    OUTPUT.write(PREFIX + json.dumps({"type": event_type, **values}, ensure_ascii=True) + "\n")
    OUTPUT.flush()


def log(message, level="info"):
    emit("log", message=redact(message), level=level)


class Console(io.TextIOBase):
    def __init__(self):
        self.buffered = ""

    def write(self, value):
        self.buffered += value.replace("\r", "\n")
        while "\n" in self.buffered:
            line, self.buffered = self.buffered.split("\n", 1)
            if line.strip():
                log(line.strip())
        if len(self.buffered) > 1500:
            log(self.buffered)
            self.buffered = ""
        return len(value)

    def flush(self):
        if self.buffered.strip():
            log(self.buffered.strip())
        self.buffered = ""


class LogBridge(logging.Handler):
    def emit(self, record):
        text = record.getMessage()
        if record.levelno >= logging.ERROR or "captcha" in text.lower():
            ISSUES.add("attention")
        if "time limit exceeded" in text.lower():
            ISSUES.add("limited")
        # The original logger can include authentication responses in INFO records.
        if "one-time" in text.lower() or "qr scanned" in text.lower():
            text = "已扫码，请在手机上确认登录。"
        log(text, "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info")


def run(payload):
    root, local, runtime = (Path(payload[key]) for key in ("root", "local", "runtime"))
    config = load_json(root / "config.json", {})
    remember_secrets(config)
    # Redirect legacy log files into this server's temporary directory.
    import utils
    real_path = utils.getRealPath
    utils.getRealPath = lambda path: str(runtime / "logs") if str(path) in ("logs", "./logs") else real_path(path)
    from logger import logger
    logger.setLevel("INFO")
    logger.addHandler(LogBridge())
    import fucker as legacy
    from utils import cookie_jar_to_list
    settings = payload["settings"]
    client = legacy.Fucker(proxies=config.get("proxies") or {}, speed=settings["speed"],
        limit=int(settings["limit"]), end_thre=settings["threshold"], tree_view=True, progressbar_view=True)
    progress_time = [0.0]

    def progress(iteration, total, prefix="", **_):
        now = time.monotonic()
        if now - progress_time[0] >= 0.5 or iteration >= total:
            progress_time[0] = now
            emit("progress", value=round(max(0, min(100, iteration / total * 100)), 1) if total else 0,
                 title=redact(prefix) or "当前视频")

    legacy.progressBar = progress
    action = payload["action"]
    cookie_sources = [runtime / "cookies.json"]
    if settings["remember"]:
        cookie_sources += [local / "cookies.json", root / "cookies.json"]
    session_exists = False
    for source in cookie_sources:
        cookies = load_json(source, [])
        if cookies:
            remember_secrets(cookies)
            try:
                client.cookies = cookies
                if client.uuid:
                    SECRETS.add(str(client.uuid))
                session_exists = True
                break
            except Exception:
                continue

    if action == "login":
        log("正在生成登录二维码…")
        client.cookies = {}
        client.login(use_qr=True, interactive=False,
                     qr_callback=lambda image: emit("qr", image="data:image/png;base64," + base64.b64encode(image).decode("ascii")))
        cookies = cookie_jar_to_list(client.cookies)
        remember_secrets(cookies)
        atomic_json(runtime / "cookies.json", cookies)
        if settings["remember"]:
            atomic_json(local / "cookies.json", cookies)
        emit("auth", logged_in=True)
        log("登录成功，正在同步课程。")
    elif not session_exists:
        emit("auth", logged_in=False)
        raise ValueError("没有可用的登录状态，请先扫码登录。")

    if action in ("login", "refresh"):
        rows, kinds, failures = [], [], []
        for kind, method, id_field in (("zhidao", client.getZhidaoList, "secret"), ("hike", client.getHikeList, "courseId")):
            try:
                result = method()
                rows += [{"id": str(c[id_field]), "name": c.get("courseName") or "未命名课程", "kind": kind} for c in result]
                kinds.append(kind)
            except Exception as error:
                failures.append(kind)
                log(("共享课" if kind == "zhidao" else "校内课") + "同步失败：" + redact(error), "warning")
        if not kinds:
            emit("auth", logged_in=False)
            raise ValueError("课程同步失败，请检查网络或重新扫码登录。")
        emit("auth", logged_in=True)
        atomic_json(runtime / "cookies.json", cookie_jar_to_list(client.cookies))
        emit("courses", courses=rows, kinds=kinds)
        message = f"已同步 {len(rows)} 门课程。" + ("部分课程类型同步失败，请查看日志。" if failures else "")
        log(message, "warning" if failures else "info")
        emit("result", message=message, error=bool(failures))
        return

    attention = 0
    for course in payload["courses"]:
        ISSUES.clear()
        key = course_key(course)
        emit("course", key=key, status="running", message="正在运行 · " + course["name"])
        log("开始课程：" + course["name"])
        try:
            if course["kind"] == "hike":
                client.fuckHikeCourse(course["id"])
            elif course["kind"] == "zhidao":
                client.fuckZhidaoCourse(course["id"])
            else:
                client.fuckCourse(course["id"])
            status = "attention" if "attention" in ISSUES else "limited" if "limited" in ISSUES else "done"
        except Exception as error:
            status = "attention"
            log("课程执行失败：" + redact(error), "error")
        attention += status == "attention"
        emit("course", key=key, status=status)
        log(course["name"] + " · " + {"done": "本次任务已运行。", "limited": "已达到本次时限。", "attention": "存在异常，请查看日志。"}[status])
    message = "本次任务已结束。课程完成情况请以平台为准。"
    if attention:
        message = f"本次任务已结束，{attention} 门课程需要查看日志。"
    emit("result", message=message, error=bool(attention))


def main():
    console = Console()
    try:
        payload = json.loads(sys.stdin.readline())
        with redirect_stdout(console), redirect_stderr(console):
            run(payload)
            console.flush()
        return 0
    except Exception as error:
        console.flush()
        log(redact(error), "error")
        emit("result", message="任务未能完成，请查看运行日志。", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
