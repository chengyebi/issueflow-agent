import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"


def _request(
    method: str,
    path: str,
    payload: dict | None = None,
) -> dict | list | None:
    token = os.environ.get("GITHUB_TOKEN")

    if not token:
        raise RuntimeError("GITHUB_TOKEN 未配置")

    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "issueflow-agent",
    }

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        url=f"{GITHUB_API_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(request, timeout=20) as response:
            response_body = response.read()

            if not response_body:
                return None

            return json.loads(response_body)

    except HTTPError as exc:
        error_body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"GitHub API 请求失败: "
            f"{method} {path}, "
            f"HTTP {exc.code}, "
            f"{error_body}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(
            f"无法连接 GitHub API: {exc.reason}"
        ) from exc


def _issue_path(
    repo: str,
    issue_number: int,
) -> str:
    parts = repo.split("/")

    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "repo 必须使用 owner/repository 格式"
        )

    if issue_number <= 0:
        raise ValueError(
            "issue_number 必须大于 0"
        )

    owner = quote(parts[0], safe="")
    repository = quote(parts[1], safe="")

    return (
        f"/repos/{owner}/{repository}"
        f"/issues/{issue_number}"
    )


def add_issue_label(
    repo: str,
    issue_number: int,
    label: str,
) -> list:
    if not label.strip():
        raise ValueError("label 不能为空")

    result = _request(
        method="POST",
        path=f"{_issue_path(repo, issue_number)}/labels",
        payload={
            "labels": [label],
        },
    )

    if not isinstance(result, list):
        raise RuntimeError(
            "GitHub 添加标签接口返回格式异常"
        )

    return result


def post_issue_comment(
    repo: str,
    issue_number: int,
    body: str,
) -> dict:
    if not body.strip():
        raise ValueError("评论内容不能为空")

    result = _request(
        method="POST",
        path=f"{_issue_path(repo, issue_number)}/comments",
        payload={
            "body": body,
        },
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            "GitHub 创建评论接口返回格式异常"
        )

    return result
