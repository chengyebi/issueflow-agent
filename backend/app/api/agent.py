from fastapi import APIRouter, HTTPException

from app.agents.workflow import IssueAgentRequest, IssueAgentResponse, run_issue_agent

router = APIRouter(tags=["agent"])


@router.post("/agent/analyze", response_model=IssueAgentResponse)
def analyze_issue_with_agent(issue: IssueAgentRequest):
    try:
        return run_issue_agent(issue)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent analysis failed: {exc}") from exc

