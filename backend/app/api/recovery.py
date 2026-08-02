from fastapi import APIRouter, HTTPException, Query

from app.services.outbox import (
    dispatch_event,
    dispatch_pending,
    requeue_failed_agent_run,
    requeue_failed_command,
)

router = APIRouter(tags=["recovery"])


@router.post("/recovery/outbox/dispatch")
def recover_outbox(limit: int = Query(default=50, ge=1, le=200)):
    results = dispatch_pending(limit)
    return {"count": len(results), "items": [result.__dict__ for result in results]}


@router.post("/recovery/agent-runs/{agent_run_id}/requeue")
def recover_agent_run(agent_run_id: int):
    try:
        key = requeue_failed_agent_run(agent_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = dispatch_event(key)
    return result.__dict__

@router.post("/recovery/github-commands/{command_id}/requeue")
def recover_github_command(command_id: int):
    try:
        key = requeue_failed_command(command_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = dispatch_event(key)
    return result.__dict__
