import sys
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.rag.embedding import (
    DisabledEmbeddingProvider,
    EmbeddingDimensionError,
    EmbeddingUnavailableError,
    FakeEmbeddingProvider,
    FastEmbedEmbeddingProvider,
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


def test_fastembed_declared_dimension_mismatch_fails_before_model_load(
    monkeypatch, tmp_path
):
    class TextEmbedding:
        @staticmethod
        def get_embedding_size(model_name):
            return 384

        def __init__(self, **kwargs):
            raise AssertionError("dimension mismatch must fail before model load")

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=TextEmbedding))
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=object))
    settings = Settings(
        embedding_provider="fastembed",
        embedding_dimension=128,
        embedding_cache_dir=str(tmp_path),
    )
    with pytest.raises(EmbeddingDimensionError, match="声明维度为 384"):
        FastEmbedEmbeddingProvider(settings)


def test_fastembed_query_prefix_is_configurable_without_changing_documents():
    provider = object.__new__(FastEmbedEmbeddingProvider)
    provider.query_prefix = "Represent this sentence: "
    provider.embed = lambda texts: texts

    assert provider.embed_query(["login crash"]) == [
        "Represent this sentence: login crash"
    ]
