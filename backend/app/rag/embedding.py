import hashlib
import math
from typing import Protocol

from app.core.config import Settings, get_settings


class EmbeddingError(RuntimeError):
    pass


class EmbeddingUnavailableError(EmbeddingError):
    pass


class EmbeddingProvider(Protocol):
    model_name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DisabledEmbeddingProvider:
    model_name = "disabled"
    dimensions = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("Embedding Provider 未配置")


class FakeEmbeddingProvider:
    """Deterministic, dependency-free provider for tests and reproducible baselines."""

    def __init__(self, dimensions: int = 16, model_name: str = "fake-hash-v1"):
        if dimensions < 2 or dimensions > 4096:
            raise ValueError("Embedding dimensions 必须在 2 到 4096 之间")
        self.dimensions = dimensions
        self.model_name = model_name
        self.call_count = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        vectors = []
        for text in texts:
            values = []
            counter = 0
            while len(values) < self.dimensions:
                digest = hashlib.sha256(
                    f"{self.model_name}:{counter}:{text}".encode("utf-8")
                ).digest()
                values.extend((byte - 127.5) / 127.5 for byte in digest)
                counter += 1
            vector = values[: self.dimensions]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def create_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(
            dimensions=settings.embedding_dimensions,
            model_name=settings.embedding_model,
        )
    if settings.embedding_provider == "disabled":
        return DisabledEmbeddingProvider()
    raise EmbeddingUnavailableError(
        f"未实现的 Embedding Provider: {settings.embedding_provider}"
    )
