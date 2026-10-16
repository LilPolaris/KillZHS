"""Opt-in notifications, delivered outside the controller lock."""

import copy
import json
import queue
import threading
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from killzhs_config import InputError

NOTIFICATION_DEFAULTS = {
    "enabled": False, "provider": "pushplus", "pushplus_token": "",
    "bark_server": "https://api.day.app", "bark_key": "",
    "events": {"finished": True, "verification": True, "auth": True, "skipped": True},
}


def validate_notifications(value, previous=None):
    if not isinstance(value, dict) or set(value) - NOTIFICATION_DEFAULTS.keys():
        raise InputError("通知设置格式不正确。")
    result = copy.deepcopy(previous or NOTIFICATION_DEFAULTS)
    for key, item in value.items():
        if key == "enabled":
            if not isinstance(item, bool):
                raise InputError("通知开关值不正确。")
        elif key == "provider":
            if item not in ("pushplus", "bark"):
                raise InputError("请选择 PushPlus 或 Bark。")
        elif key == "events":
            if not isinstance(item, dict) or set(item) - result["events"].keys() or any(not isinstance(v, bool) for v in item.values()):
                raise InputError("通知事件设置不正确。")
            result["events"].update(item)
            continue
        else:
            if not isinstance(item, str) or len(item) > 500 or any(ord(c) < 32 for c in item):
                raise InputError("通知地址或密钥格式不正确。")
            item = item.strip()
            if key == "bark_server":
                try:
                    parsed = urlsplit(item)
                    parsed.port
                    valid = parsed.scheme in ("http", "https") and parsed.hostname and not (
                        parsed.username or parsed.password or parsed.query or parsed.fragment)
                except ValueError:
                    valid = False
                if not valid:
                    raise InputError("Bark 服务器需为 http(s) 地址，请把设备密钥单独填写。")
                item = item.rstrip("/")
        result[key] = item
    secret = result["pushplus_token"] if result["provider"] == "pushplus" else result["bark_key"]
    if result["enabled"] and not secret:
        raise InputError("启用通知前，请先填写所选渠道的密钥。")
    return result


def public_notifications(value):
    result = copy.deepcopy(value)
    for key in ("pushplus_token", "bark_key"):
        result["has_" + key] = bool(result.pop(key))
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        # Never forward a token to a redirected server.
        return None


def send_notification(config, title, body):
    if config["provider"] == "pushplus":
        url = "https://www.pushplus.plus/send"
        payload = {"token": config["pushplus_token"], "title": title, "content": body, "template": "txt"}
    else:
        url = config["bark_server"] + "/push"
        payload = {"device_key": config["bark_key"], "title": title, "body": body, "group": "KillZHS"}
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=8) as response:
        result = json.loads(response.read(65536))
    if not isinstance(result, dict) or result.get("code") != 200:
        raise ValueError("服务未接受通知")


class Notifier:
    def __init__(self, report, sender=None):
        self.report = report
        self.sender = sender or send_notification
        self.queue = queue.Queue(maxsize=32)
        self.thread = None
        self.closed = False

    def submit(self, config, event, title, body, *, test=False, report=None):
        if self.closed or (not test and (not config["enabled"] or not config["events"].get(event, False))):
            return False
        try:
            self.queue.put_nowait((copy.deepcopy(config), title, body, test, report or self.report))
        except queue.Full:
            self.report("通知队列已满，本次未发送。", "warning")
            return False
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
        return True

    def run(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                config, title, body, test, report = item
                try:
                    self.sender(config, title, body)
                    report(("测试通知" if test else "通知") + "已提交到推送服务，实际送达以接收端为准。", "info")
                except Exception:
                    # Provider errors and URLs can contain credentials.
                    report("通知发送失败，请检查渠道配置、网络及服务额度；任务不受影响。", "warning")
            finally:
                self.queue.task_done()

    def close(self):
        self.closed = True
        if self.thread:
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                return
            self.thread.join(timeout=1)
