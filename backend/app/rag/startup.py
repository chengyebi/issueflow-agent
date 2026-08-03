from app.core.config import get_settings
from app.rag.embedding import FastEmbedEmbeddingProvider, get_embedding_provider

_embedding_runtime_status: dict = {"status": "not_checked"}


def validate_embedding_runtime() -> dict:
    global _embedding_runtime_status
    settings = get_settings()
    if settings.embedding_provider == "disabled":
        _embedding_runtime_status = {"status": "disabled"}
        return _embedding_runtime_status
    if settings.embedding_provider == "fake":
        _embedding_runtime_status = {
            "status": "test_only",
            "provider": "fake",
            "dimensions": settings.embedding_dimension,
        }
        return _embedding_runtime_status
    provider = get_embedding_provider()
    if not isinstance(provider, FastEmbedEmbeddingProvider):
        raise RuntimeError("启动校验只允许已实现的本地 Embedding Provider")
    details = provider.validate()
    _embedding_runtime_status = {"status": "ready", **details}
    return _embedding_runtime_status


def get_embedding_runtime_status() -> dict:
    return dict(_embedding_runtime_status)
