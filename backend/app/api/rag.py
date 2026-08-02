from typing import Literal

from fastapi import APIRouter, Query

from app.rag.retrieval import HybridRetriever

router = APIRouter(tags=["rag"])


@router.get("/historical-issues/search")
def search_historical_issues(
    repo: str,
    query: str,
    mode: Literal["lexical", "vector", "hybrid"] = "hybrid",
    top_k: int = Query(default=5, ge=1, le=50),
    exclude_issue_number: int | None = None,
):
    result = HybridRetriever().search(
        repo,
        query,
        "",
        mode=mode,
        top_k=top_k,
        exclude_issue_number=exclude_issue_number,
    )
    return result
