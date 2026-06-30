from fastapi import FastAPI

app = FastAPI(title="IssueFlow Agent")

@app.get("/health")
def health_check():
    return {"status": "ok"}