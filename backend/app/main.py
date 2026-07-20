import os
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException, Request
from psycopg.rows import dict_row
from pydantic import BaseModel,ValidationError
from app.github_webhook import verify_github_signature
from app.agent import (
    IssueAgentRequest,
    IssueAgentResponse,
    run_issue_agent,
)
from app.job_queue import enqueue_issue_agent_run

app = FastAPI(title="IssueFlow Agent")
DATABASE_URL = os.environ["DATABASE_URL"]
GITHUB_WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]

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

class GitHubIssueActionPayload(BaseModel):
    action: str

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

def save_webhook_delivery(
    delivery_id: str,
    event_name: str,
    payload_body: bytes,
) -> bool:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhook_deliveries (
                    delivery_id,
                    event_name,
                    raw_payload
                )
                VALUES (
                    %s,
                    %s,
                    %s::jsonb
                )
                ON CONFLICT (delivery_id) DO NOTHING
                RETURNING id;
                """,
                (
                    delivery_id,
                    event_name,
                    payload_body.decode("utf-8"),
                ),
            )

            row = cur.fetchone()

            return row is not None

def save_webhook_and_issue_event(
    delivery_id: str,
    event_name: str,
    payload_body: bytes,
    event: InternalIssueEvent,
) -> tuple[bool, int | None]:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhook_deliveries (
                    delivery_id,
                    event_name,
                    raw_payload
                )
                VALUES (
                    %s,
                    %s,
                    %s::jsonb
                )
                ON CONFLICT (delivery_id) DO NOTHING
                RETURNING id;
                """,
                (
                    delivery_id,
                    event_name,
                    payload_body.decode("utf-8"),
                ),
            )

            delivery_row = cur.fetchone()

            if delivery_row is None:
                return False, None

            webhook_delivery_id = delivery_row[0]

            cur.execute(
                """
                INSERT INTO issue_events (
                    source,
                    event_type,
                    repo,
                    action,
                    issue_number,
                    issue_title,
                    issue_body,
                    webhook_delivery_id
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
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
                    webhook_delivery_id,
                ),
            )

            issue_row = cur.fetchone()

            if issue_row is None:
                raise RuntimeError(
                    "插入 Issue 事件后没有返回 ID"
                )

            return True, issue_row[0]
def list_issue_events() -> list[dict]:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                    id,
                    source,
                    event_type,
                    repo,
                    action,
                    issue_number,
                    issue_title,
                    issue_body,
                    created_at
                FROM issue_events
                ORDER BY id DESC
                LIMIT 20;
                """)
            return cur.fetchall()

def create_agent_run(issue_event_id: int) -> int:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_runs (issue_event_id)
                VALUES (%s)
                RETURNING id;
                """,
                (issue_event_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise RuntimeError("创建 Agent Run 后没有返回 ID")

            return row[0]


def save_agent_run_job_id(
    agent_run_id: int,
    rq_job_id: str,
) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_runs
                SET rq_job_id = %s
                WHERE id = %s;
                """,
                (rq_job_id, agent_run_id),
            )


def mark_agent_run_failed(
    agent_run_id: int,
    error_message: str,
) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_runs
                SET
                    status = 'failed',
                    finished_at = NOW(),
                    error_message = %s
                WHERE id = %s;
                """,
                (error_message, agent_run_id),
            )

@app.get("/health")
def get_health():
    return {"status": "ok"}


@app.get("/events")
def get_events():
    events = list_issue_events()

    return {
        "count": len(events),
        "items": events,
    }


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

@app.post("/webhooks/github")
async def receive_github_webhook(request: Request):
    payload_body = await request.body()

    signature_header = request.headers.get(
        "X-Hub-Signature-256"
    )

    signature_valid=verify_github_signature(
        payload_body=payload_body,
        secret=GITHUB_WEBHOOK_SECRET,
        signature_header = signature_header,
    )

    if not signature_valid:
        raise HTTPException(
            status_code = 401,
            detail = "Invalid GitHub signature",
        )
    event_name = request.headers.get("X-GitHub-Event")

    if event_name is None:
        raise HTTPException(
            status_code=400,
            detail="Missing GitHub event header",
        )

    if event_name != "issues":
        return{
        "status" : "ignored",
        "event" : event_name,
        }

    delivery_id=request.headers.get("X-GitHub-Delivery")

    if not delivery_id:
        raise HTTPException(
            status_code=400,
            detail="Missing GitHub delivery header",
        )
    try:
        action_payload = GitHubIssueActionPayload.model_validate_json(
        payload_body
    )
    except ValidationError as exc:
        raise HTTPException(
        status_code=422,
        detail="Invalid GitHub issues payload",
    ) from exc

    supported_actions = {
    "opened",
    "edited",
    "closed",
    "reopened",
}

    # 不支持的 action：只保存原始 Webhook，不生成 Issue Event
    if action_payload.action not in supported_actions:
        is_new_delivery = save_webhook_delivery(
            delivery_id=delivery_id,
            event_name=event_name,
            payload_body=payload_body,
        )

        if not is_new_delivery:
            return {
                "status": "duplicate",
                "event": event_name,
                "action": action_payload.action,
                "delivery_id": delivery_id,
            }

        return {
            "status": "ignored",
            "event": event_name,
            "action": action_payload.action,
            "delivery_id": delivery_id,
        }

    # 支持的 action：解析完整的 GitHub Issue 事件
    try:
        github_event = GitHubIssueEvent.model_validate_json(
            payload_body
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid GitHub issue event payload",
        ) from exc

    internal_event = normalize_github_issue_event(
        github_event
    )

    # 在同一个数据库事务中保存两张表
    is_new_delivery, issue_event_id = (
        save_webhook_and_issue_event(
            delivery_id=delivery_id,
            event_name=event_name,
            payload_body=payload_body,
            event=internal_event,
        )
    )

    if not is_new_delivery:
        return {
            "status": "duplicate",
            "event": event_name,
            "action": action_payload.action,
            "delivery_id": delivery_id,
        }
    if issue_event_id is None:
        raise RuntimeError("新的 Webhook 没有生成 Issue Event ID")

    agent_run_id = create_agent_run(issue_event_id)

    try:
        rq_job_id = enqueue_issue_agent_run(agent_run_id)
        save_agent_run_job_id(
            agent_run_id=agent_run_id,
            rq_job_id=rq_job_id,
        )
    except Exception as exc:
        mark_agent_run_failed(
            agent_run_id=agent_run_id,
            error_message=f"RQ enqueue failed: {exc}",
        )

        raise HTTPException(
            status_code=503,
            detail="Agent job enqueue failed",
        ) from exc
    return {
        "status": "accepted",
        "event": event_name,
        "action": action_payload.action,
        "delivery_id": delivery_id,
        "issue_event_id": issue_event_id,
        "agent_run_id": agent_run_id,
        "rq_job_id": rq_job_id,
    }

@app.post(
    "/agent/analyze",
    response_model=IssueAgentResponse,
)
def analyze_issue_with_agent(
    issue: IssueAgentRequest,
):
    try:
        return run_issue_agent(issue)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Agent analysis failed: {exc}",
        ) from exc
