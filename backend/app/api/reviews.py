from fastapi import APIRouter, HTTPException

from app.models.reviews import ReviewDecisionRequest
from app.services.exceptions import ConflictError, NotFoundError
from app.services.reviews import decide_review_task, list_review_tasks
from app.workers.queue import enqueue_review_commands

router = APIRouter(tags=["reviews"])
ALLOWED_STATUSES = {"pending", "approved", "rejected"}


@router.get("/review-tasks")
def get_review_tasks(status: str | None = None):
    if status is not None and status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid review status")
    items = list_review_tasks(status)
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
    if result["updated_command_ids"]:
        try:
            rq_job_id = enqueue_review_commands(review_task_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Review 已批准，但 GitHub 命令投递失败: {type(exc).__name__}",
            ) from exc
    return {**result, "rq_job_id": rq_job_id}


@router.post("/review-tasks/{review_task_id}/reject")
def reject_review_task(review_task_id: int, request: ReviewDecisionRequest):
    return _decide(review_task_id, "rejected", request)

