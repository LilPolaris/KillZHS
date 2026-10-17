"""Environment adaptation must not hide platform-header or body regressions."""

import copy

import pytest

from .compare import request_differences


def test_requests_compression_defaults_follow_the_interpreter_without_mutating_recording(monkeypatch):
    monkeypatch.setattr("requests.utils.DEFAULT_ACCEPT_ENCODING", "gzip, deflate")
    expected = [{"url": "https://example.test", "headers": {"accept-encoding": "gzip, deflate, zstd"},
                 "body": {"unchanged": 1}}]
    before = copy.deepcopy(expected)
    actual = copy.deepcopy(expected)
    actual[0]["headers"]["accept-encoding"] = "gzip, deflate"
    assert request_differences(expected, actual) == []
    assert expected == before


@pytest.mark.parametrize("change", ["unsupported-decoder", "extra-header", "body"])
def test_default_adaptation_still_reports_unsupported_encoding_headers_and_body_changes(monkeypatch, change):
    monkeypatch.setattr("requests.utils.DEFAULT_ACCEPT_ENCODING", "gzip, deflate")
    expected = [{"url": "https://example.test", "headers": {"accept-encoding": "gzip, deflate, zstd"},
                 "body": {"unchanged": 1}}]
    actual = copy.deepcopy(expected)
    actual[0]["headers"]["accept-encoding"] = "gzip, deflate"
    if change == "unsupported-decoder":
        actual[0]["headers"]["accept-encoding"] = "gzip, deflate, zstd"
    elif change == "extra-header":
        actual[0]["headers"]["authorization"] = "unexpected"
    else:
        actual[0]["body"]["unchanged"] = 2
    assert request_differences(expected, actual)


def test_explicit_platform_encoding_is_never_normalized(monkeypatch):
    monkeypatch.setattr("requests.utils.DEFAULT_ACCEPT_ENCODING", "gzip, deflate")
    expected = [{"url": "https://example.test", "headers": {"accept-encoding": "gzip, deflate, br"}}]
    actual = [{"url": "https://example.test", "headers": {"accept-encoding": "gzip, deflate"}}]
    assert request_differences(expected, actual)
