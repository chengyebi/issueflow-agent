import pytest

from app.core.config import Settings
from app.rag.embedding import (
    DisabledEmbeddingProvider,
    EmbeddingUnavailableError,
    FakeEmbeddingProvider,
    create_embedding_provider,
)


def test_fake_embedding_is_deterministic_and_configurable():
    provider = FakeEmbeddingProvider(dimensions=8)
    first = provider.embed(["same text"])[0]
    second = provider.embed(["same text"])[0]
    other = provider.embed(["different text"])[0]
    assert len(first) == 8
    assert first == second
    assert first != other
    assert provider.call_count == 3


def test_fake_embedding_rejects_invalid_dimensions():
    with pytest.raises(ValueError):
        FakeEmbeddingProvider(dimensions=1)


def test_disabled_provider_fails_explicitly():
    with pytest.raises(EmbeddingUnavailableError):
        DisabledEmbeddingProvider().embed(["query"])


def test_unknown_embedding_provider_is_not_silently_selected():
    settings = Settings(embedding_provider="paid-provider-not-installed")
    with pytest.raises(EmbeddingUnavailableError, match="未实现"):
        create_embedding_provider(settings)
