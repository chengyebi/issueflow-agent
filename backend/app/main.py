from fastapi import FastAPI

app = FastAPI(title="IssueFlow Agent")


@app.get("/health")
def get_health():
    return {"status": "ok"}


@app.post("/issues")
def post_issue():
    return {"issue_number": "1", "issue_title": "first issue"}
