from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

SupportedAction = Literal["opened", "edited", "closed", "reopened"]


class IssueCreate(BaseModel):
    number: int
    title: str
    body: str
    repo: str
    action: str


class RepositoryPayload(BaseModel):
    full_name: str


class GitHubIssuePayload(BaseModel):
    number: int
    title: str
    body: str | None = None
    labels: list[dict] = Field(default_factory=list)
    state: Literal["open", "closed"] = "open"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    pull_request: dict | None = None


class GitHubIssueActionPayload(BaseModel):
    action: str


class GitHubIssueEvent(BaseModel):
    action: SupportedAction
    repository: RepositoryPayload
    issue: GitHubIssuePayload


class InternalIssueEvent(BaseModel):
    source: Literal["github"]
    event_type: Literal["issue"]
    repo: str
    action: SupportedAction
    issue_number: int
    issue_title: str
    issue_body: str
    labels: list[str] = Field(default_factory=list)
    state: Literal["open", "closed"] = "open"
    github_created_at: datetime
    github_updated_at: datetime


def normalize_github_issue_event(event: GitHubIssueEvent) -> InternalIssueEvent:
    return InternalIssueEvent(
        source="github",
        event_type="issue",
        repo=event.repository.full_name,
        action=event.action,
        issue_number=event.issue.number,
        issue_title=event.issue.title,
        issue_body=event.issue.body or "",
        labels=[
            item.get("name", "")
            for item in event.issue.labels
            if isinstance(item, dict) and item.get("name")
        ],
        state=event.issue.state,
        github_created_at=(
            event.issue.created_at or event.issue.updated_at or datetime.now(timezone.utc)
        ),
        github_updated_at=event.issue.updated_at or datetime.now(timezone.utc),
    )
