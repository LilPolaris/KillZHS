"""One isolated job; communicate with the UI using structured stdout events."""

import base64
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import logging
import os
from pathlib import Path
import sys
import time

from webui import PREFIX, atomic_json, course_key, load_json, normalize_course
from killzhs_config import AI_DEFAULTS
from killzhs_privacy import collect_secrets, redact as redact_text
from killzhs_runtime import TaskControl, TaskStopped
from killzhs_verification import VerificationProbe

OUTPUT = sys.stdout
SECRETS = set()
ISSUES = set()


class AIBudgetExceeded(BaseException):
    """Escape legacy retry/continue handlers when the user budget is exhausted."""


def redact(value):
    return redact_text(value, SECRETS)


def remember_secrets(value, key=""):
    SECRETS.update(collect_secrets(value, key))


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
        diagnostic = getattr(record, "verification_diagnostic", False)
        if not diagnostic and (record.levelno >= logging.ERROR or "captcha" in text.lower()):
            ISSUES.add("attention")
        if not diagnostic and "captcha" in text.lower():
            ISSUES.add("verification")
        if not diagnostic and "time limit exceeded" in text.lower():
            ISSUES.add("limited")
        # The original logger can include authentication responses in INFO records.
        if not diagnostic and ("one-time" in text.lower() or "qr scanned" in text.lower()):
            text = "已扫码，请在手机上确认登录。"
        log(text, "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info")


def run(payload):
    root, local, runtime = (Path(payload[key]) for key in ("root", "local", "runtime"))
    config = load_json(root / "config.json", {})
    ai = payload.get("ai", AI_DEFAULTS)
    remember_secrets(ai)
    remember_secrets(config)
    action = payload["action"]
    if action == "test_ai":
        import requests
        if ai["use_zhidao_ai"]:
            raise ValueError("平台 AI 使用登录会话，不支持独立模型列表检查。")
        provider = ai["openai"]
        if not provider["api_key"]:
            raise ValueError("请先保存自定义服务的 API Key。")
        response = requests.get(provider["api_base"].rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + provider["api_key"]}, timeout=15)
        response.raise_for_status()
        models = response.json().get("data", [])
        available = any(isinstance(model, dict) and model.get("id") == provider["model_name"] for model in models)
        message = "模型服务可访问，已找到所填模型。" if available else "模型服务可访问，列表未包含所填模型；请与服务商核对模型名称。"
        log(message, "info" if available else "warning")
        emit("result", message=message, error=False)
        return
    # Redirect legacy log files into this server's temporary directory.
    import utils
    real_path = utils.getRealPath
    def user_path(path):
        relative = str(path).replace("\\", "/").removeprefix("./")
        if relative == "logs":
            return str(runtime / "logs")
        if relative.split("/")[0] in ("aiexamAnswer", "AiDownloadCache"):
            target = (local / relative).resolve()
            if target.is_relative_to(local.resolve()):
                return str(target)
        return real_path(path)
    utils.getRealPath = user_path
    from logger import logger
    logger.setLevel("INFO")
    logger.addHandler(LogBridge())
    import fucker as legacy
    from utils import cookie_jar_to_list
    if action == "diagnose":
        emit("result", message="运行环境检查通过，Python 依赖与课程模块均可加载。", error=False)
        return
    settings = payload["settings"]
    client = legacy.Fucker(proxies=config.get("proxies") or {}, speed=settings["speed"],
        limit=int(settings["limit"]), end_thre=settings["threshold"], tree_view=True, progressbar_view=True)
    control = TaskControl(payload.get("stop_file"), payload.get("deadline"))
    client.task_control = control
    progress_time = [0.0]

    def progress(iteration, total, prefix="", **_):
        now = time.monotonic()
        if now - progress_time[0] >= 0.5 or iteration >= total:
            progress_time[0] = now
            waiting = prefix == "pause a minute"
            emit("progress", value=round(max(0, min(100, iteration / total * 100)), 1) if total else 0,
                 title="短暂暂停，等待继续" if waiting else redact(prefix) or "当前视频",
                 phase="waiting" if waiting else "video", remaining=max(0, round(total - iteration)) if waiting else None)

    legacy.progressBar = progress
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
        rows, kinds, failures, incomplete = [], [], [], []
        methods = [("zhidao", client.getZhidaoList, "secret"), ("hike", client.getHikeList, "courseId")]
        if hasattr(client, "getZhidaoAiList"):
            methods.append(("ai", client.getZhidaoAiList, "courseId"))
        for kind, method, id_field in methods:
            try:
                result = method()
                if not isinstance(result, list):
                    raise ValueError("课程接口返回了无法识别的结构，可稍后重试或手动添加课程。")
                incoming = []
                for course in result:
                    try:
                        incoming.append(normalize_course({"id": str(course[id_field]),
                            "name": course.get("courseName") or course.get("name") or "未命名课程",
                            "kind": kind, **({"class_id": str(course["classId"])} if kind == "ai" else {})}))
                    except (KeyError, TypeError, ValueError):
                        if kind not in incomplete:
                            incomplete.append(kind)
                        log("已跳过一条不完整的课程数据，其余课程继续同步。", "warning")
                rows += incoming
                kinds.append(kind)
            except Exception as error:
                failures.append(kind)
                log({"zhidao": "共享课", "hike": "校内课", "ai": "AI 课程"}[kind] + "同步失败：" + redact(error), "warning")
        if not kinds:
            emit("auth", logged_in=False)
            raise ValueError("课程同步失败，请检查网络或重新扫码登录。")
        emit("auth", logged_in=True)
        atomic_json(runtime / "cookies.json", cookie_jar_to_list(client.cookies))
        emit("courses", courses=rows, kinds=kinds, incomplete_kinds=incomplete)
        partial = bool(failures or incomplete)
        message = f"已同步 {len(rows)} 门课程。" + ("部分数据同步不完整，已保留原有课程，请查看日志。" if partial else "")
        log(message, "warning" if partial else "info")
        emit("result", message=message, error=partial)
        return

    # Count actual generation requests, including the original client's retries.
    import requests
    request_count = [0]
    original_request = requests.sessions.Session.request

    def bounded_request(session, method, url, *args, **kwargs):
        control.check()
        if "/chat/completions" in str(url) or "/qa/platform/stream" in str(url):
            if request_count[0] >= ai["max_requests"]:
                raise AIBudgetExceeded()
            request_count[0] += 1
            emit("ai_usage", count=request_count[0])
        kwargs.setdefault("timeout", 30)
        return original_request(session, method, url, *args, **kwargs)

    requests.sessions.Session.request = bounded_request
    client.diagnostic_secrets = SECRETS
    client.batch_started_monotonic = time.monotonic() - max(0, time.time() - payload.get("started_at", time.time()))

    def probe_report(**values):
        if values["phase"] == "waiting":
            ISSUES.add("verification")
        elif values["phase"] == "observing" and values.get("normal_reports") == 0:
            ISSUES.discard("verification")
        emit("verification_probe", worker_pid=os.getpid(), **values)

    probe = VerificationProbe(client, control,
        Path(payload["probe_signal_file"]) if payload.get("probe_signal_file") else None,
        payload.get("task_id"), enabled=payload.get("verify_probe_enabled") is True and os.environ.get("KILLZHS_VERIFY_PROBE") == "1",
        report=probe_report, log=log)
    client.verification_probe = probe
    attention = 0
    for course in payload["courses"]:
        probe.course_kind = course["kind"]
        client.verification_video = None
        control.begin_course(settings["limit"])
        control.check()
        ISSUES.clear()
        key = course_key(course)
        emit("course", key=key, status="running", message="正在运行 · " + course["name"])
        log("开始课程：" + course["name"])
        try:
            if course["kind"] == "ai":
                log("AI 课程模式：" + ("跳过测验。" if ai["no_exam"] else "允许 AI 处理测验，生成请求上限 " + str(ai["max_requests"]) + " 次。"))
                client.fuckAiCourse(int(course["id"]), int(course["class_id"]), aiConfig=ai, no_exam=ai["no_exam"])
            elif course["kind"] == "hike":
                client.fuckHikeCourse(course["id"])
            elif course["kind"] == "zhidao":
                client.fuckZhidaoCourse(course["id"])
            else:
                client.fuckCourse(course["id"])
            status = "attention" if "attention" in ISSUES else "limited" if "limited" in ISSUES else "done"
        except TaskStopped as stopped:
            if stopped.reason != "course_limit":
                raise
            status = "limited"
            log("已达到本门课程的实际运行时限，已尝试保存最后进度。", "warning")
        except Exception as error:
            status = "attention"
            if type(error).__name__ == "CaptchaException":
                ISSUES.add("verification")
            log("课程执行失败：" + redact(error), "error")
        finally:
            probe.finish("course_ended")
        attention += status == "attention"
        emit("course", key=key, status=status)
        log(course["name"] + " · " + {"done": "本次任务已运行。", "limited": "已达到本次时限。", "attention": "存在异常，请查看日志。"}[status])
        if "verification" in ISSUES:
            emit("result", message="需要手动验证，本批次已停止。请先打开知到官方页面完成验证，再手动开始任务。",
                 error=True, reason="verification")
            return
    message = "本次任务已结束。课程完成情况请以平台为准。"
    if attention:
        message = f"本次任务已结束，{attention} 门课程需要查看日志。"
    emit("result", message=message, error=bool(attention))


def main():
    console = Console()
    try:
        payload = json.loads(sys.stdin.read())
        with redirect_stdout(console), redirect_stderr(console):
            run(payload)
            console.flush()
        return 0
    except AIBudgetExceeded:
        console.flush()
        log("已达到本次 AI 请求上限，任务停止。", "warning")
        emit("result", message="已达到 AI 请求上限，请查看任务记录。", error=True)
        return 1
    except TaskStopped as stopped:
        console.flush()
        status = "limited" if stopped.reason == "limited" else "stopped"
        message = ("已达到本次总时长" if status == "limited" else "已按请求停止任务") + "，已尝试完成收尾；进度以平台实际接收为准。"
        reason = None
        if "verification" in ISSUES:
            message += " 收尾时需要手动验证，请先到知到官方页面处理后再开始任务。"
            reason = "verification"
        log(message, "warning")
        emit("result", message=message, status=status, error=False, reason=reason)
        return 0
    except Exception as error:
        console.flush()
        log(redact(error), "error")
        emit("result", message="任务未能完成，请查看运行日志。", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
