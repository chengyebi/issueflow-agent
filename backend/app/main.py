from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.agent import router as agent_router
from app.api.evals import router as evals_router
from app.api.health import router as health_router
from app.api.issues import router as issues_router
from app.api.observability import router as observability_router
from app.api.rag import router as rag_router
from app.api.recovery import router as recovery_router
from app.api.reviews import router as reviews_router
from app.api.webhooks import router as webhooks_router
from app.rag.startup import validate_embedding_runtime


@asynccontextmanager
async def lifespan(application: FastAPI):
    validate_embedding_runtime()
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="IssueFlow Agent", lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(issues_router)
    application.include_router(webhooks_router)
    application.include_router(agent_router)
    application.include_router(reviews_router)
    application.include_router(observability_router)
    application.include_router(recovery_router)
    application.include_router(evals_router)
    application.include_router(rag_router)
    return application


app = create_app()
