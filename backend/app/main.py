from fastapi import FastAPI

from app.api.agent import router as agent_router
from app.api.health import router as health_router
from app.api.issues import router as issues_router
from app.api.reviews import router as reviews_router
from app.api.webhooks import router as webhooks_router


def create_app() -> FastAPI:
    application = FastAPI(title="IssueFlow Agent")
    application.include_router(health_router)
    application.include_router(issues_router)
    application.include_router(webhooks_router)
    application.include_router(agent_router)
    application.include_router(reviews_router)
    return application


app = create_app()
