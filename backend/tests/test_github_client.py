import pytest
import requests

from app.services import github


def test_github_api_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    response = type("Response", (), {
        "status_code": 500,
        "headers": {},
        "ok": False,
        "content": b'{"message":"sensitive payload"}',
    })()
    monkeypatch.setattr(github.requests, "request", lambda *args, **kwargs: response)
    monkeypatch.setattr(github.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError) as exc_info:
        github.add_issue_label("o/r", 1, "bug")
    assert "HTTP 500" in str(exc_info.value)
    assert "sensitive payload" not in str(exc_info.value)
    assert "test-token" not in str(exc_info.value)
    assert exc_info.value.retry_safe is True


def test_ambiguous_network_failure_is_not_auto_retryable(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    monkeypatch.setattr(
        github.requests,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("connection reset")
        ),
    )
    with pytest.raises(github.GitHubRequestError) as exc_info:
        github.post_issue_comment("o/r", 1, "reply")
    assert exc_info.value.retry_safe is False
    assert "connection reset" not in str(exc_info.value)


def test_github_write_guard_blocks_request_before_network(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_WRITE_ENABLED", "false")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    monkeypatch.setattr(
        github.requests,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no network write")),
    )
    with pytest.raises(RuntimeError, match="已禁止 GitHub 写请求"):
        github.add_issue_label("o/r", 1, "bug")


def test_read_only_get_retries_transient_network_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    calls = 0

    class Response:
        status_code = 200
        headers = {}
        ok = True
        content = b'{"number": 1}'

        def json(self):
            return {"number": 1}

    def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise requests.ConnectionError("temporary reset")
        return Response()

    monkeypatch.setattr(github.requests, "request", request)
    monkeypatch.setattr(github.time, "sleep", lambda _seconds: None)
    assert github.get_repository_issue("o/r", 1)["number"] == 1
    assert calls == 3
