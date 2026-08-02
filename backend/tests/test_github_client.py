from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from app.services import github


def test_github_api_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    error = HTTPError(
        "https://api.github.test/repos/o/r/issues/1/labels",
        500,
        "failure",
        {},
        BytesIO(b'{"message":"sensitive payload"}'),
    )
    monkeypatch.setattr(github, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))
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
        github,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("connection reset")),
    )
    with pytest.raises(github.GitHubRequestError) as exc_info:
        github.post_issue_comment("o/r", 1, "reply")
    assert exc_info.value.retry_safe is False
    assert "connection reset" not in str(exc_info.value)
