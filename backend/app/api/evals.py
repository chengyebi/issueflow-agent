from fastapi import APIRouter, HTTPException, Query

from app.services.evals import get_eval_report, list_eval_reports

router = APIRouter(tags=["eval"])


@router.get("/eval/reports")
def eval_reports(limit: int = Query(default=50, ge=1, le=200)):
    items = list_eval_reports(limit)
    return {"count": len(items), "items": items}


@router.get("/eval/reports/{eval_id}")
def eval_report_detail(eval_id: str):
    report = get_eval_report(eval_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Eval report not found")
    return report
