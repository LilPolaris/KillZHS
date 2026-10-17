"""Opt-in PushPlus / Bark delivery on a background queue, outside the controller lock."""

import json
import queue
import threading
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import NotificationConfig


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None  # Never forward a token to a redirected server.


def send_notification(config, title, body):
    if isinstance(config, dict):
        config = NotificationConfig.model_validate(config)
    if config.provider == "pushplus":
        url = "https://www.pushplus.plus/send"
        payload = {"token": config.pushplus_token, "title": title, "content": body, "template": "txt"}
    else:
        url = config.bark_server + "/push"
        payload = {"device_key": config.bark_key, "title": title, "body": body, "group": "KillZHS"}
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=8) as response:
        result = json.loads(response.read(65536))
    if not isinstance(result, dict) or result.get("code") != 200:
        raise ValueError("服务未接受通知")


class Notifier:
    """At most 32 queued messages; failures are reported, never retried, never raised."""

    def __init__(self, report, sender=None):
        self.report = report
        self.sender = sender or send_notification
        self.queue = queue.Queue(maxsize=32)
        self.thread = None
        self.closed = False
        self.lock = threading.Lock()

    def submit(self, config, event, title, body, *, test=False, report=None):
        if isinstance(config, dict):
            config = NotificationConfig.model_validate(config)
        with self.lock:
            if self.closed or (not test and (not config.enabled or not getattr(config.events, event, False))):
                return False
            try:
                self.queue.put_nowait((config.model_copy(deep=True), title, body, test, report or self.report))
            except queue.Full:
                full = True
            else:
                full = False
                if self.thread is None:
                    self.thread = threading.Thread(target=self.run, name="notifications", daemon=True)
                    self.thread.start()
        if full:
            self._report(self.report, "通知队列已满，本次未发送。", "warning")
        return not full

    def run(self):
        while True:
            try:
                item = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self.closed:
                    return
                continue
            try:
                if item is None:
                    return
                config, title, body, test, report = item
                try:
                    self.sender(config, title, body)
                except Exception:
                    # Provider errors and URLs can contain credentials.
                    self._report(report, "通知发送失败，请检查渠道配置、网络及服务额度；任务不受影响。", "warning")
                else:
                    self._report(report, ("测试通知" if test else "通知") +
                                 "已提交到推送服务，实际送达以接收端为准。", "info")
            finally:
                self.queue.task_done()

    @staticmethod
    def _report(report, message, level):
        try:
            report(message, level)
        except Exception:
            # A callback failing must not strand later messages or retry delivery.
            pass

    def close(self):
        with self.lock:
            self.closed = True
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
