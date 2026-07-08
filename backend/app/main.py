from fastapi import FastAPI
from pydantic import BaseModel
from typing import Literal

app = FastAPI(title="IssueFlow Agent")


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
    body: str


class GitHubIssueEvent(BaseModel):
    action: Literal["opened", "edited", "closed", "reopened"]
    repository: RepositoryPayload
    issue: GitHubIssuePayload


class InternalIssueEvent(BaseModel):
    source: Literal["github"]
    event_type: Literal["issue"]
    repo: str
    action: Literal["opened", "edited", "closed", "reopened"]
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
        issue_body=event.issue.body,
    )


@app.get("/health")
def get_health():
    return {"status": "ok"}


@app.post("/issues")
def post_issue(issue: IssueCreate):
    return {
        "issue_number": issue.number,
        "issue_title": issue.title,
        "issue_body": issue.body,
        "issue_repo": issue.repo,
        "issue_action": issue.action,
    }


@app.post("/dev/events/github")
def receive_github_event(event: GitHubIssueEvent):
    return normalize_github_issue_event(event)
