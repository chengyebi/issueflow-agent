from fastapi import APIRouter

from app.rag.startup import get_embedding_runtime_status

router = APIRouter(tags=["system"])


@router.get("/health")
def get_health():
    return {"status": "ok"}


@router.get("/health/embedding")
def get_embedding_health():
    return get_embedding_runtime_status()
