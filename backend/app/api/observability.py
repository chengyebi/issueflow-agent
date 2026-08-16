from fastapi import APIRouter, HTTPException, Query

from app.services.automation_metrics import aggregate_automation_metrics
from app.services.traces import aggregate_run_metrics, get_trace, list_traces

router = APIRouter(tags=["observability"])
RUN_STATUSES = {"pending", "running", "completed", "failed"}


@router.get("/traces")
def query_traces(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    if status is not None and status not in RUN_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid agent run status")
    items = list_traces(status, limit, offset)
    return {"count": len(items), "items": items}


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str):
    trace = get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace


@router.get("/metrics/agent-runs")
def agent_run_metrics():
    return aggregate_run_metrics()


@router.get("/metrics/automation")
def automation_metrics():
    return aggregate_automation_metrics()
