import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.connection import connect
from app.rag.chunking import chunk_issue_text
from app.rag.embedding import FastEmbedEmbeddingProvider
from app.rag.indexing import embed_historical_issue
from app.rag.repository import PostgresHistoricalIssueRepository
from app.rag.retrieval import HybridRetriever
from app.rag.schema import HistoricalIssue

pytestmark = [
    pytest.mark.integration,
    pytest.mark.local_embedding,
    pytest.mark.skipif(
        os.getenv("RUN_LOCAL_EMBEDDING_TESTS") != "1",
        reason="requires explicit local model download/cache opt-in",
    ),
]


def test_approved_bge_model_outputs_384_dimensions():
    provider = FastEmbedEmbeddingProvider(
        Settings(
            embedding_provider="fastembed",
            embedding_model="BAAI/bge-small-en-v1.5",
            embedding_dimension=384,
            embedding_cache_dir=os.getenv(
                "EMBEDDING_CACHE_DIR", "/var/cache/issueflow/fastembed"
            ),
            embedding_local_files_only=os.getenv("HF_HUB_OFFLINE") == "1",
        )
    )
    vector = provider.embed_query(["Title: color theme crash\nBody:\nmenu closes\nLabels: bug"])[0]
    assert len(vector) == 384
    assert provider.last_observations[0].max_input_tokens == 512

    chunked = chunk_issue_text(
        "Terminal rendering regression",
        " ".join(["rendering freezes after resize"] * 300),
        provider,
        chunk_size=384,
        overlap=64,
        max_chunks=16,
    )
    assert len(chunked.chunks) > 1
    assert all(len(provider.tokenize(chunk.text)) <= 384 for chunk in chunked.chunks)
    assert chunked.original_token_count >= chunked.stored_token_count


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="requires a migrated PostgreSQL database",
)
def test_approved_bge_model_round_trips_through_pgvector():
    provider = FastEmbedEmbeddingProvider(
        Settings(
            embedding_provider="fastembed",
            embedding_model="BAAI/bge-small-en-v1.5",
            embedding_dimension=384,
            embedding_cache_dir=os.getenv(
                "EMBEDDING_CACHE_DIR", "/var/cache/issueflow/fastembed"
            ),
            embedding_local_files_only=True,
        )
    )
    repository = PostgresHistoricalIssueRepository()
    repo = f"integration/fastembed-{uuid4()}"
    try:
        stored = repository.upsert(
            HistoricalIssue(
                repo=repo,
                issue_number=1,
                title="Color theme menu closes immediately",
                body="Opening the theme picker causes it to flash and disappear.",
                labels=["bug", "workbench"],
                state="closed",
                github_created_at=datetime.now(timezone.utc),
                github_updated_at=datetime.now(timezone.utc),
            )
        )
        result = embed_historical_issue(
            stored.historical_issue_id, repository=repository, provider=provider
        )
        search = HybridRetriever(repository, provider).search(
            repo,
            "Theme picker flashes closed",
            "The color theme menu disappears",
            labels=["bug", "workbench"],
            mode="vector",
        )
        assert result["status"] == "embedded"
        assert result["dimensions"] == 384
        assert search.candidates[0].issue_number == 1
        assert search.embedding_model == "BAAI/bge-small-en-v1.5"
    finally:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM historical_issues WHERE repo = %s", (repo,))
