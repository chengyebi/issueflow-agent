from typing import Literal

from pydantic import BaseModel

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


def normalize_github_issue_event(event: GitHubIssueEvent) -> InternalIssueEvent:
    return InternalIssueEvent(
        source="github",
        event_type="issue",
        repo=event.repository.full_name,
        action=event.action,
        issue_number=event.issue.number,
        issue_title=event.issue.title,
        issue_body=event.issue.body or "",
    )

