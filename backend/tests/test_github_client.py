from io import BytesIO
from urllib.error import HTTPError

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

