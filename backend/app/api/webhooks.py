from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.security import verify_github_signature
from app.models.issues import (
    GitHubIssueActionPayload,
    GitHubIssueEvent,
    normalize_github_issue_event,
)
from app.services.events import (
    accept_issue_delivery,
    mark_agent_run_failed,
    save_agent_run_job_id,
    save_webhook_delivery,
)
from app.workers.queue import enqueue_issue_agent_run

router = APIRouter(tags=["webhooks"])
SUPPORTED_ACTIONS = {"opened", "edited", "closed", "reopened"}


@router.post("/webhooks/github")
async def receive_github_webhook(request: Request):
    payload_body = await request.body()
    settings = get_settings()
    if not verify_github_signature(
        payload_body,
        settings.github_webhook_secret.get_secret_value(),
        request.headers.get("X-Hub-Signature-256"),
    ):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")

    event_name = request.headers.get("X-GitHub-Event")
    if event_name is None:
        raise HTTPException(status_code=400, detail="Missing GitHub event header")
    if event_name != "issues":
        return {"status": "ignored", "event": event_name}

    delivery_id = request.headers.get("X-GitHub-Delivery")
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing GitHub delivery header")
    try:
        action_payload = GitHubIssueActionPayload.model_validate_json(payload_body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid GitHub issues payload"
        ) from exc

    if action_payload.action not in SUPPORTED_ACTIONS:
        is_new = save_webhook_delivery(delivery_id, event_name, payload_body)
        return {
            "status": "ignored" if is_new else "duplicate",
            "event": event_name,
            "action": action_payload.action,
            "delivery_id": delivery_id,
        }

    try:
        github_event = GitHubIssueEvent.model_validate_json(payload_body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid GitHub issue event payload"
        ) from exc

    accepted = accept_issue_delivery(
        delivery_id,
        event_name,
        payload_body,
        normalize_github_issue_event(github_event),
    )
    if not accepted.is_new:
        return {
            "status": "duplicate",
            "event": event_name,
            "action": action_payload.action,
            "delivery_id": delivery_id,
        }
    if accepted.issue_event_id is None or accepted.agent_run_id is None:
        raise RuntimeError("新的 Webhook 没有生成任务 ID")

    try:
        rq_job_id = enqueue_issue_agent_run(accepted.agent_run_id)
        save_agent_run_job_id(accepted.agent_run_id, rq_job_id)
    except Exception as exc:
        mark_agent_run_failed(
            accepted.agent_run_id, f"RQ enqueue failed: {type(exc).__name__}"
        )
        raise HTTPException(status_code=503, detail="Agent job enqueue failed") from exc

    return {
        "status": "accepted",
        "event": event_name,
        "action": action_payload.action,
        "delivery_id": delivery_id,
        "issue_event_id": accepted.issue_event_id,
        "agent_run_id": accepted.agent_run_id,
        "rq_job_id": rq_job_id,
    }

