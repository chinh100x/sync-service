import json

from sync_service import slack


class _FakeHTTPResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_no_webhook_configured_never_attempts_a_request(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    def raising_urlopen(*_args, **_kwargs):
        raise AssertionError("urlopen must not be called when no webhook is configured")

    monkeypatch.setattr("urllib.request.urlopen", raising_urlopen)

    assert slack.post("hello") is False


def test_successful_post_returns_true_and_sends_the_message_as_json(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T000/B000/xxx")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = slack.post("hello world")

    assert result is True
    assert captured["url"] == "https://hooks.slack.example/T000/B000/xxx"
    assert captured["body"] == {"text": "hello world"}


def test_non_2xx_response_returns_false_not_an_exception(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T000/B000/xxx")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeHTTPResponse(500))

    assert slack.post("hello") is False


def test_network_error_is_swallowed_never_raised(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T000/B000/xxx")

    def raising_urlopen(*_args, **_kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", raising_urlopen)

    assert slack.post("hello") is False  # not an exception propagating out
