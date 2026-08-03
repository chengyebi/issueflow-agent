import json
import time
from urllib.parse import quote, urlencode

import requests

from app.core.config import get_settings


class GitHubRequestError(RuntimeError):
    def __init__(self, message: str, *, retry_safe: bool):
        super().__init__(message)
        self.retry_safe = retry_safe


class GitHubRateLimitError(GitHubRequestError):
    pass


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
    if method != "GET" and not settings.github_write_enabled:
        raise RuntimeError("GITHUB_WRITE_ENABLED=false，已禁止 GitHub 写请求")
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
    for attempt in range(5):
        try:
            response = requests.request(
                method,
                f"{settings.github_api_url}{path}",
                data=data,
                headers=headers,
                timeout=(10, 20),
            )
            if response.status_code in {403, 429} and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or response.headers.get("Retry-After") is not None
            ):
                reset = response.headers.get("X-RateLimit-Reset", "unknown")
                raise GitHubRateLimitError(
                    f"GitHub API 速率限制已触发，reset={reset}", retry_safe=True
                )
            if method == "GET" and response.status_code >= 500 and attempt < 4:
                time.sleep(0.5 * (2**attempt))
                continue
            if not response.ok:
                # Do not propagate response bodies; they can contain user data.
                raise GitHubRequestError(
                    f"GitHub API 请求失败: {method} {path}, HTTP {response.status_code}",
                    retry_safe=True,
                )
            return response.json() if response.content else None
        except requests.RequestException as exc:
            if method == "GET" and attempt < 4:
                time.sleep(0.5 * (2**attempt))
                continue
            # A lost response can be ambiguous for non-idempotent comment creation.
            raise GitHubRequestError(
                "无法确认 GitHub API 请求结果", retry_safe=method == "GET"
            ) from exc
    raise AssertionError("unreachable")


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


def get_repository_issue(repo: str, issue_number: int) -> dict:
    result = _request("GET", _issue_path(repo, issue_number))
    if not isinstance(result, dict):
        raise RuntimeError("GitHub Issue 详情接口返回格式异常")
    return result


def _list_issue_resource(
    repo: str,
    issue_number: int,
    resource: str,
    *,
    max_items: int = 100,
) -> list[dict]:
    if resource not in {"comments", "events", "timeline"}:
        raise ValueError("不支持的 Issue 只读资源")
    items: list[dict] = []
    page = 1
    while len(items) < max_items:
        per_page = min(100, max_items - len(items))
        result = _request(
            "GET",
            f"{_issue_path(repo, issue_number)}/{resource}"
            f"?per_page={per_page}&page={page}",
        )
        if not isinstance(result, list):
            raise RuntimeError(f"GitHub Issue {resource} 接口返回格式异常")
        items.extend(item for item in result if isinstance(item, dict))
        if len(result) < per_page:
            break
        page += 1
    return items[:max_items]


def list_issue_comments(repo: str, issue_number: int, *, max_items: int = 100) -> list[dict]:
    return _list_issue_resource(repo, issue_number, "comments", max_items=max_items)


def list_issue_events(repo: str, issue_number: int, *, max_items: int = 100) -> list[dict]:
    return _list_issue_resource(repo, issue_number, "events", max_items=max_items)


def list_issue_timeline(
    repo: str, issue_number: int, *, max_items: int = 100
) -> list[dict]:
    return _list_issue_resource(repo, issue_number, "timeline", max_items=max_items)


def list_repository_labels(repo: str, *, max_items: int = 500) -> list[dict]:
    if not 1 <= max_items <= 1000:
        raise ValueError("GitHub Label 列表上限必须在 1 到 1000 之间")
    base_path = _issue_path(repo, 1).rsplit("/issues/1", 1)[0]
    items: list[dict] = []
    page = 1
    while len(items) < max_items:
        per_page = min(100, max_items - len(items))
        result = _request(
            "GET", f"{base_path}/labels?per_page={per_page}&page={page}"
        )
        if not isinstance(result, list):
            raise RuntimeError("GitHub Label 列表接口返回格式异常")
        items.extend(item for item in result if isinstance(item, dict))
        if len(result) < per_page:
            break
        page += 1
    return items[:max_items]


def search_issues(
    query: str,
    *,
    max_items: int = 100,
    sort: str = "updated",
    order: str = "desc",
) -> list[dict]:
    if not 1 <= max_items <= 1000:
        raise ValueError("GitHub Search 单次任务上限必须在 1 到 1000 之间")
    if sort not in {"created", "updated", "comments"} or order not in {"asc", "desc"}:
        raise ValueError("GitHub Search 排序参数无效")
    items: list[dict] = []
    page = 1
    while len(items) < max_items:
        per_page = min(100, max_items - len(items))
        path = "/search/issues?" + urlencode(
            {"q": query, "sort": sort, "order": order, "per_page": per_page, "page": page}
        )
        result = _request("GET", path)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError("GitHub Issue Search 接口返回格式异常")
        page_items = [item for item in result["items"] if isinstance(item, dict)]
        items.extend(page_items)
        if len(page_items) < per_page:
            break
        page += 1
    return items[:max_items]
