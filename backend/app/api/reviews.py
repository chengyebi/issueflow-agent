from fastapi import APIRouter, Depends, HTTPException

from app.core.review_auth import require_review_admin
from app.models.reviews import ReviewDecisionRequest
from app.services.exceptions import ConflictError, NotFoundError
from app.services.outbox import dispatch_event
from app.services.reviews import decide_review_task, list_review_tasks

router = APIRouter(
    tags=["reviews"],
    dependencies=[Depends(require_review_admin)],
)
ALLOWED_STATUSES = {"pending", "approved", "rejected"}


@router.get("/review-tasks")
def get_review_tasks(status: str | None = None, reason_code: str | None = None):
    if status is not None and status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid review status")
    items = list_review_tasks(status, reason_code=reason_code)
    return {"count": len(items), "items": items}


def _decide(review_task_id: int, decision: str, request: ReviewDecisionRequest):
    try:
        return decide_review_task(
            review_task_id, decision, request.reviewer, request.review_note
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/review-tasks/{review_task_id}/approve")
def approve_review_task(review_task_id: int, request: ReviewDecisionRequest):
    result = _decide(review_task_id, "approved", request)
    rq_job_id = None
    recovery_pending = False

    if result["updated_command_ids"]:
        dispatch = dispatch_event(result["outbox_event_key"])
        rq_job_id = dispatch.rq_job_id
        recovery_pending = dispatch.recovery_pending

    return {
        **result,
        "rq_job_id": rq_job_id,
        "recovery_pending": recovery_pending,
    }


@router.post("/review-tasks/{review_task_id}/reject")
def reject_review_task(review_task_id: int, request: ReviewDecisionRequest):
    return _decide(review_task_id, "rejected", request)
