from fastapi import FastAPI
from pydantic import BaseModel

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
    action: str
    repository: RepositoryPayload
    issue: GitHubIssuePayload


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
    return {
        "event_source": "github",
        "repo": event.repository.full_name,
        "action": event.action,
        "issue_number": event.issue.number,
        "issue_title": event.issue.title,
        "issue_body": event.issue.body,
    }
