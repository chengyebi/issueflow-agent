import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.core.config import get_settings


class GitHubRequestError(RuntimeError):
    def __init__(self, message: str, *, retry_safe: bool):
        super().__init__(message)
        self.retry_safe = retry_safe


def _issue_path(repo: str, issue_number: int) -> str:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repo 必须使用 owner/repository 格式")
    if issue_number <= 0:
        raise ValueError("issue_number 必须大于 0")
    return (
        f"/repos/{quote(parts[0], safe='')}/{quote(parts[1], safe='')}"
        f"/issues/{issue_number}"
    )


def _request(method: str, path: str, payload: dict | None = None):
    settings = get_settings()
    token = settings.github_token.get_secret_value() if settings.github_token else ""
    if not token:
        raise RuntimeError("GITHUB_TOKEN 未配置")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": settings.github_api_version,
        "User-Agent": "issueflow-agent",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{settings.github_api_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as exc:
        # Do not propagate response bodies; they can contain user or credential data.
        raise GitHubRequestError(
            f"GitHub API 请求失败: {method} {path}, HTTP {exc.code}",
            retry_safe=True,
        ) from exc
    except URLError as exc:
        # A lost response can be ambiguous for non-idempotent comment creation.
        raise GitHubRequestError("无法确认 GitHub API 请求结果", retry_safe=False) from exc


def add_issue_label(repo: str, issue_number: int, label: str) -> list:
    if not label.strip():
        raise ValueError("label 不能为空")
    result = _request(
        "POST", f"{_issue_path(repo, issue_number)}/labels", {"labels": [label]}
    )
    if not isinstance(result, list):
        raise RuntimeError("GitHub 添加标签接口返回格式异常")
    return result


def post_issue_comment(repo: str, issue_number: int, body: str) -> dict:
    if not body.strip():
        raise ValueError("评论内容不能为空")
    result = _request(
        "POST", f"{_issue_path(repo, issue_number)}/comments", {"body": body}
    )
    if not isinstance(result, dict):
        raise RuntimeError("GitHub 创建评论接口返回格式异常")
    return result


def list_repository_issues(
    repo: str, *, state: str = "all", per_page: int = 100
):
    if state not in {"open", "closed", "all"}:
        raise ValueError("state 必须是 open、closed 或 all")
    if not 1 <= per_page <= 100:
        raise ValueError("per_page 必须在 1 到 100 之间")
    base_path = _issue_path(repo, 1).rsplit("/issues/1", 1)[0]
    page = 1
    while True:
        result = _request(
            "GET",
            f"{base_path}/issues?state={state}&per_page={per_page}&page={page}",
        )
        if not isinstance(result, list):
            raise RuntimeError("GitHub Issues 列表接口返回格式异常")
        yield from result
        if len(result) < per_page:
            break
        page += 1
