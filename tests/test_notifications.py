import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from killzhs.models import NotificationConfig, update_notifications
from killzhs.notifications import NoRedirect, Notifier, send_notification


def config():
    return update_notifications(NotificationConfig(), {"enabled": True, "pushplus_token": "private-token",
                                                       "bark_key": "private-device"})


@pytest.mark.parametrize("provider", ["pushplus", "bark"])
@pytest.mark.parametrize("as_dict", [False, True])
def test_provider_payloads_keep_credentials_out_of_url_and_disable_redirects(monkeypatch, provider, as_dict):
    settings = config().model_copy(update={"provider": provider})
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"code":200}'
    build = MagicMock(return_value=opener)
    monkeypatch.setattr("killzhs.notifications.build_opener", build)
    send_notification(settings.model_dump() if as_dict else settings, "title", "body")
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert request.method == "POST" and "private" not in request.full_url
    assert opener.open.call_args.kwargs["timeout"] == 8
    assert payload["token" if provider == "pushplus" else "device_key"] == (
        "private-token" if provider == "pushplus" else "private-device")
    assert isinstance(build.call_args.args[0], NoRedirect)
    assert NoRedirect().redirect_request() is None


@pytest.mark.parametrize("response", [b'{"code":400}', b'[]', b'null', b'invalid'])
def test_provider_business_and_json_errors_are_failures(monkeypatch, response):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = response
    monkeypatch.setattr("killzhs.notifications.build_opener", lambda *_: opener)
    with pytest.raises(ValueError):
        send_notification(config(), "title", "body")


def test_delivery_is_nonblocking_snapshot_is_independent_and_errors_do_not_leak():
    entered, release, reported = threading.Event(), threading.Event(), threading.Event()
    messages, received = [], []

    def sender(settings, *_):
        entered.set()
        assert release.wait(3)
        received.append(settings.pushplus_token)
        raise RuntimeError("private-token https://secret.test/private")

    def report(message, level):
        messages.append((message, level))
        reported.set()

    notifier = Notifier(report, sender=sender)
    settings = config()
    try:
        started = time.monotonic()
        assert notifier.submit(settings, "finished", "title", "body")
        assert time.monotonic() - started < 0.5
        assert entered.wait(2)
        settings.pushplus_token = "changed-token"
        release.set()
        assert reported.wait(2)
        assert received == ["private-token"]
        assert "private-token" not in str(messages) and "secret.test" not in str(messages)
        assert messages[0][1] == "warning" and "失败" in messages[0][0]
        settings.events.finished = False
        assert not notifier.submit(settings, "finished", "title", "body")
    finally:
        release.set()
        notifier.close()
    assert not notifier.thread.is_alive() and notifier.queue.unfinished_tasks == 0


def test_notifier_accepts_dict_config_and_uses_the_original_report_callback():
    messages, original = [], []
    notifier = Notifier(lambda *args: messages.append(args), sender=lambda *_: None)
    try:
        assert notifier.submit(config().model_dump(), "finished", "title", "body",
                               report=lambda *args: original.append(args))
        notifier.queue.join()
        assert not messages and "已提交" in original[0][0]
        assert not notifier.submit(NotificationConfig(), "finished", "title", "body")
        assert not notifier.submit(config(), "unknown", "title", "body")
        assert notifier.submit(NotificationConfig(), "finished", "title", "body", test=True)
        notifier.queue.join()
        assert "测试通知" in messages[0][0]
    finally:
        notifier.close()
    assert not notifier.submit(config(), "finished", "title", "body")


def test_full_queue_close_drains_and_exits_without_sentinel_space():
    entered, release = threading.Event(), threading.Event()
    delivered, reports = [], []

    def sender(settings, title, body):
        entered.set()
        release.wait(4)
        delivered.append(title)

    notifier = Notifier(lambda *args: reports.append(args), sender=sender)
    try:
        assert notifier.submit(config(), "finished", "first", "body")
        assert entered.wait(2)
        for number in range(32):
            assert notifier.submit(config(), "finished", str(number), "body")
        assert not notifier.submit(config(), "finished", "overflow", "body")
        assert "队列已满" in reports[0][0]
        notifier.close()
        release.set()
        notifier.thread.join(3)
        assert not notifier.thread.is_alive()
        assert len(delivered) == 33 and notifier.queue.unfinished_tasks == 0
    finally:
        release.set()
        notifier.close()


def test_concurrent_submit_starts_one_thread_and_bad_reports_do_not_strand_queue():
    delivered = []

    def broken_report(*_):
        raise OSError("closed log")

    notifier = Notifier(broken_report, sender=lambda _, title, __: delivered.append(title))
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            accepted = list(workers.map(lambda n: notifier.submit(config(), "finished", str(n), "body"), range(12)))
        assert all(accepted)
    finally:
        notifier.close()
    assert len(delivered) == 12 and notifier.queue.unfinished_tasks == 0
    assert not notifier.thread.is_alive()
