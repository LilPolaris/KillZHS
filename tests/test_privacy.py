import json

from killzhs.privacy import HIDDEN, collect_secrets, redact


def test_nested_secret_collection_includes_cookie_and_notification_keys():
    values = {"password": "private-password", "openai": {"api_key": "private-key"},
              "cookies": [{"value": "private-cookie"}], "bark_key": "private-bark",
              "pushplus_token": "private-push", "Authorization": "private-auth", "name": "visible"}
    secrets = collect_secrets(values)
    assert len(secrets) == 6 and "visible" not in secrets
    assert "private-" not in redact(json.dumps(values), secrets)


def test_redaction_handles_overlaps_short_keys_empty_collections_and_ansi():
    assert redact("\x1b[31mabcde abc\x1b[0m", {"abc", "abcde", ""}) == HIDDEN + " " + HIDDEN
    assert collect_secrets({"token": "abc"}) == {"abc"}
    assert collect_secrets({}) == collect_secrets([]) == set()
    assert len(redact("x" * 2000)) == 1500


def test_unknown_credentials_in_queries_headers_and_json_are_hidden():
    text = ('https://user:pass@example.test/path?pwd=one-time#fragment '
            'token=abc "api_key": "a secret phrase" oncePassword=temporary '
            'Authorization: Bearer unknown-bearer')
    result = redact(text)
    for secret in ("user:pass", "one-time", "fragment", "abc", "a secret phrase", "temporary", "unknown-bearer"):
        assert secret not in result
    assert "https://" in result and "example.test/path" in result


def test_redaction_preserves_useful_nonsecret_context():
    assert redact("课程开始：https://example.test/course/12") == "课程开始：https://example.test/course/12"
