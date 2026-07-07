from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="IssueFlow Agent")


class IssueCreate(BaseModel):
    number: int
    title: str
    body: str


@app.get("/health")
def get_health():
    return {"status": "ok"}


@app.post("/issues")
def post_issue(issue: IssueCreate):
    return {
        "issue_number": issue.number,
        "issue_title": issue.title,
        "issue_body": issue.body,
    }
