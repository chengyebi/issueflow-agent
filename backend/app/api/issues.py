from fastapi import APIRouter

from app.models.issues import GitHubIssueEvent, IssueCreate, normalize_github_issue_event
from app.services.events import list_issue_events, save_issue_event

router = APIRouter(tags=["issues"])


@router.get("/events")
def get_events():
    events = list_issue_events()
    return {"count": len(events), "items": events}


@router.post("/issues")
def post_issue(issue: IssueCreate):
    return {
        "issue_number": issue.number,
        "issue_title": issue.title,
        "issue_body": issue.body,
        "issue_repo": issue.repo,
        "issue_action": issue.action,
    }


@router.post("/dev/events/github")
def receive_github_event(event: GitHubIssueEvent):
    internal_event = normalize_github_issue_event(event)
    return {"event_id": save_issue_event(internal_event), "event": internal_event}

