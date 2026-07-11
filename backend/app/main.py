import os
from typing import Literal

import psycopg
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="IssueFlow Agent")
DATABASE_URL = os.environ["DATABASE_URL"]


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


def save_issue_event(event: InternalIssueEvent) -> int:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
            INSERT INTO issue_events(
                source,
                event_type,
                repo,
                action,
                issue_number,
                issue_title,
                issue_body
            ) VALUES(
                %s,%s,%s,%s,%s,%s,%s
            )
            RETURNING id;
            """,
                (
                    event.source,
                    event.event_type,
                    event.repo,
                    event.action,
                    event.issue_number,
                    event.issue_title,
                    event.issue_body,
                ),
            )
            row = cur.fetchone()

            if row is None:
                raise RuntimeError("插入Issue事件后没有返回ID")

            return row[0]


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
    internal_event = normalize_github_issue_event(event)
    event_id = save_issue_event(internal_event)

    return {
        "event_id": event_id,
        "event": internal_event,
    }
