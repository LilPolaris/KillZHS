"""The eleven worker-independent verification-probe intents from the legacy suite."""

import json
import time
from unittest.mock import Mock

import pytest
from requests.adapters import HTTPAdapter, Retry

from killzhs.platform.http import CaptchaRequired, Http
from killzhs.platform.verification import VerificationProbe
from killzhs.runtime import TaskControl, TaskStopped
from tests.protocol.fake_platform import make_response
from tests.test_platform_runtime import shared_video

URL = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"


@pytest.fixture
def prepare(monkeypatch):
    clients = []
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})

    def make(*, enabled=True, code=0):
        control = TaskControl()
        http = Http(control=control)
        clients.append(http)
        http.uuid = "private-account"
        http.batch_started = time.monotonic() - 5
        http.video = {"course_id": "course", "video_id": 10, "played_time": 12.5}
        shared = Mock(contexts={"course": {"course": {"recruitId": 7}, "videos": {10: {"id": 5, "lessonId": 6}}}})
        shared.study_info.side_effect = [
            {"lv": {"5": {"studyTotalTime": 10, "watchState": 0}}},
            {"lv": {"5": {"studyTotalTime": 25, "watchState": 0}}}]
        http.api = Mock(return_value={"code": code, "data": {}})
        events, logs = [], []
        clock = Mock(now=0, on_sleep=lambda: None)

        def sleep(seconds):
            clock.now += seconds
            clock.on_sleep()

        confirmed = [None]
        probe = VerificationProbe(http, shared, control, lambda: confirmed[0], "a" * 32, enabled=enabled,
            report=lambda **event: events.append(event), log=lambda *row: logs.append(row),
            clock=lambda: clock.now, sleep=sleep, wait_seconds=2)
        http.probe = probe
        probe.course_kind = "zhidao"
        clock.on_sleep = lambda: confirmed.__setitem__(0, probe.active)
        return http, shared, probe, clock, events, confirmed

    yield make
    for http in clients:
        http.session.close()


def test_default_diagnostics_redact_payload_and_do_not_retry(prepare, caplog):
    http, _, probe, _, _, _ = prepare(enabled=False)
    http.session.cookies.set("session", "private-cookie")
    http.api.return_value = {"code": -12, "message": "captcha required private-cookie request-secret private-account"}
    with pytest.raises(CaptchaRequired):
        http.zhidao(URL + "?token=hidden", {"token": "request-secret"}, encrypt=False, timestamp=False)
    http.api.assert_called_once()
    detail = json.loads(next(record.message.split("：", 1)[1] for record in caplog.records
                             if getattr(record, "diagnostic", False)))
    assert detail["path"] == "/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"
    assert detail["video_id"] == "10" and detail["played_time"] == 12.5
    for secret in ("private-cookie", "request-secret", "private-account", "?token"):
        assert secret not in json.dumps(detail)
    assert probe.active is None


def test_manual_confirmation_replays_frozen_envelope_once_and_observes_three_reports(prepare):
    http, shared, probe, clock, events, confirmed = prepare()
    form = {"secretStr": "original-payload", "dateFormate": 123000}
    captured = probe.capture(URL, form, "POST", "form")
    form["secretStr"] = "mutated-after-failure"

    def confirm():
        http.api.assert_not_called()
        confirmed[0] = probe.active

    clock.on_sleep = confirm
    assert probe.handle(captured)["code"] == 0
    http.api.assert_called_once_with(URL, {"secretStr": "original-payload", "dateFormate": 123000},
                                     method="POST", content_type="form")
    assert probe.observation["normal_reports"] == 0
    for _ in range(3):
        probe.success(URL)
    assert events[-1]["normal_reports"] == 3
    assert events[-1]["progress_delta_seconds"] == 15
    assert events[-1]["outcome"] == "three_normal_reports"
    assert shared.study_info.call_count == 2


def test_real_query_bridge_can_recover_without_marking_captcha_as_fatal(prepare, caplog):
    http, _, probe, _, _, _ = prepare()
    http.api.side_effect = [{"code": -12, "message": "captcha required"}, {"code": 0, "data": {"ok": True}}]
    result = http.zhidao(URL, {"unchanged": "payload"}, encrypt=False, timestamp=False)
    assert result["data"]["ok"]
    assert http.api.call_count == 2
    assert http.api.call_args_list[0] == http.api.call_args_list[1]
    assert "verification" not in http.issues
    assert not [record for record in caplog.records if record.levelno >= 40]
    assert probe.observation is not None


def test_old_or_wrong_signal_times_out_without_writes(prepare):
    http, _, probe, clock, events, confirmed = prepare()
    clock.on_sleep = lambda: confirmed.__setitem__(0, "old-probe")
    assert probe.handle(probe.capture(URL, {}, "POST", "form")) is None
    http.api.assert_not_called()
    assert events[-1]["outcome"] == "timeout"


@pytest.mark.parametrize("reason", ["stopped", "course_limit"])
def test_stop_and_time_limit_interrupt_wait_without_progress_flush(prepare, reason):
    http, _, probe, clock, _, _ = prepare()

    def stop():
        if reason == "stopped":
            http.control.stop_signal = lambda: "stopped"
        else:
            http.control.course_deadline = time.monotonic() - 1

    clock.on_sleep = stop
    runner = shared_video(http.control)
    runner.http = http
    flush = Mock()
    with pytest.raises(TaskStopped) as caught:
        try:
            probe.handle(probe.capture(URL, {}, "POST", "form"))
        except TaskStopped:
            runner.flush_after_stop(flush)
            raise
    assert caught.value.reason == reason
    http.api.assert_not_called()
    flush.assert_not_called()


def test_changed_cookie_rejects_comparison_without_rotating_tokens(prepare):
    http, shared, probe, clock, events, confirmed = prepare()
    captured = probe.capture(URL, {}, "POST", "form")

    def confirm():
        http.session.cookies.set("changed", "new-cookie")
        confirmed[0] = probe.active

    clock.on_sleep = confirm
    assert probe.handle(captured) is None
    http.api.assert_not_called()
    shared.prelearning.assert_not_called()
    assert events[-1]["outcome"] == "context_changed"


def test_rejection_is_not_repeated_or_promoted_to_another_level(prepare):
    http, _, probe, _, events, _ = prepare(code=-12)
    assert probe.handle(probe.capture(URL, {}, "POST", "form")) is None
    http.api.assert_called_once()
    assert events[-1]["outcome"] == "original_request_rejected"


def test_cleanup_and_nonshared_endpoints_never_wait(prepare):
    http, _, probe, _, _, _ = prepare()
    assert probe.capture("https://example.test/other", {}, "POST", "form") is None
    with http.control.cleanup():
        assert probe.capture(URL, {}, "POST", "form") is None
    probe.course_kind = "ai"
    assert probe.capture(URL, {}, "POST", "form") is None


def test_popup_captcha_propagates_without_an_additional_progress_report(monkeypatch):
    runner = shared_video()
    runner.pointers.return_value = {"questionPoint": [{"timeSec": 1, "questionIds": [1]}]}
    runner.popup = Mock(side_effect=CaptchaRequired("captcha required"))
    runner.report = Mock()
    monkeypatch.setattr("killzhs.platform.shared.time.sleep", lambda _: None)
    monkeypatch.setattr("killzhs.platform.shared.random.random", lambda: 1)
    with pytest.raises(CaptchaRequired):
        runner.video("course", 1)
    runner.report.assert_not_called()


@pytest.mark.parametrize("status", [500, 307])
def test_replay_has_one_wire_attempt_and_does_not_follow_redirects(prepare, monkeypatch, status):
    http, _, probe, _, _, _ = prepare()
    # Exercise real Session.send and Http.api without opening a socket. The adapter
    # sees the retry policy it would pass to urllib3; a redirect is a real Response.
    http.api = Http.api.__get__(http)
    adapter = http.session.get_adapter(URL)
    retries = Retry(total=2, status_forcelist=[500], allowed_methods={"POST"})
    adapter.max_retries = retries
    hits = []

    def send(instance, request, **kwargs):
        hits.append(request.url)
        assert instance.max_retries.total == 0
        response = make_response(request, {"code": -12}, status=status)
        if status == 307:
            response.headers["Location"] = "https://example.test/unexpected-replay"
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert probe.handle(probe.capture(URL, {"test": "body"}, "POST", "form")) is None
    assert hits == [URL]
    assert adapter.max_retries is retries
