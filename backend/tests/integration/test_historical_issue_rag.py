import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.connection import connect
from app.rag.embedding import FakeEmbeddingProvider
from app.rag.indexing import embed_historical_issue, embed_historical_issue_chunks
from app.rag.repository import PostgresHistoricalIssueRepository
from app.rag.retrieval import HybridRetriever
from app.rag.schema import HistoricalIssue
from app.rag.sync import sync_repository_issues

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="requires a migrated PostgreSQL database"
)


def _issue(repo: str, number: int, title: str, body: str) -> HistoricalIssue:
    return HistoricalIssue(
        repo=repo,
        issue_number=number,
        title=title,
        body=body,
        labels=["bug"],
        state="closed",
        github_created_at=datetime.now(timezone.utc),
        github_updated_at=datetime.now(timezone.utc),
    )


def _cleanup(*repos: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM issue_sync_runs WHERE repo = ANY(%s)", (list(repos),))
        cur.execute("DELETE FROM historical_issues WHERE repo = ANY(%s)", (list(repos),))


def test_repo_issue_upsert_is_idempotent_and_unchanged_skips_embedding():
    repo_name = f"integration/idempotent-{uuid4()}"
    repository = PostgresHistoricalIssueRepository()
    provider = FakeEmbeddingProvider(dimensions=16)
    try:
        first = repository.upsert(_issue(repo_name, 1, "Login returns 500", "bad password"))
        assert first.created is True
        outcome = embed_historical_issue(
            first.historical_issue_id, repository=repository, provider=provider
        )
        assert outcome["status"] == "embedded"
        assert provider.call_count == 1

        second = repository.upsert(_issue(repo_name, 1, "Login returns 500", "bad password"))
        assert second.created is False
        assert second.content_changed is False
        assert second.embedding_needed is False
        outcome = embed_historical_issue(
            second.historical_issue_id, repository=repository, provider=provider
        )
        assert outcome["status"] == "unchanged"
        assert provider.call_count == 1

        changed = repository.upsert(
            _issue(repo_name, 1, "Login returns 500", "bad password on macOS")
        )
        assert changed.content_changed is True
        assert changed.embedding_needed is True
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM historical_issues WHERE repo = %s AND issue_number = 1",
                (repo_name,),
            )
            assert cur.fetchone()[0] == 1
    finally:
        _cleanup(repo_name)


def test_postgres_search_is_repo_isolated_for_lexical_vector_and_hybrid():
    repo_a = f"integration/repo-a-{uuid4()}"
    repo_b = f"integration/repo-b-{uuid4()}"
    repository = PostgresHistoricalIssueRepository()
    provider = FakeEmbeddingProvider(dimensions=16)
    try:
        issue_a = repository.upsert(
            _issue(repo_a, 10, "Login endpoint returns 500", "wrong password crashes")
        )
        issue_b = repository.upsert(
            _issue(repo_b, 10, "Login endpoint returns 500", "same text other repo")
        )
        embed_historical_issue(issue_a.historical_issue_id, repository=repository, provider=provider)
        embed_historical_issue(issue_b.historical_issue_id, repository=repository, provider=provider)
        retriever = HybridRetriever(repository, provider)
        for mode in ("lexical", "vector", "hybrid"):
            result = retriever.search(
                repo_a, "Login endpoint returns 500", "wrong password crashes", mode=mode
            )
            assert result.candidates
            assert {candidate.repo for candidate in result.candidates} == {repo_a}
            assert result.candidates[0].issue_number == 10
    finally:
        _cleanup(repo_a, repo_b)


def test_backfill_skips_pull_requests_and_does_not_reembed_unchanged_content():
    repo_name = f"integration/sync-{uuid4()}"
    provider = FakeEmbeddingProvider(dimensions=16)
    payloads = [
        {
            "number": 1,
            "title": "CSV export",
            "body": "add CSV download",
            "labels": [{"name": "enhancement"}],
            "state": "open",
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
        },
        {
            "number": 2,
            "title": "PR",
            "body": "code change",
            "state": "open",
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "pull_request": {"url": "https://api.github.test/pulls/2"},
        },
    ]

    def fetcher(repo, **kwargs):
        assert repo == repo_name
        return payloads

    try:
        first = sync_repository_issues(
            repo_name, fetcher=fetcher, embedding_provider=provider
        )
        second = sync_repository_issues(
            repo_name, fetcher=fetcher, embedding_provider=provider
        )
        assert first["skipped_pull_request_count"] == 1
        assert first["embedded_count"] == 1
        assert second["embedded_count"] == 0
        assert second["unchanged_count"] == 1
        assert provider.call_count == 1
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT issue_number FROM historical_issues WHERE repo = %s", (repo_name,))
            assert [row[0] for row in cur.fetchall()] == [1]
    finally:
        _cleanup(repo_name)


def _pathological_long_query() -> str:
    # A run of '-' (the exclusion operator) inside the first LEXICAL_TSQUERY_MAX_CHARS
    # chars plus a trailing flood of dashes makes websearch_to_tsquery blow its parser
    # stack ("tsquery stack too small") unless the input is bounded and dash runs are
    # collapsed. Probe sits inside the bounded prefix so recall of head content is kept.
    phrase = (
        "when building the target triple the linker fails to resolve the codegen "
        "configuration for the selected backend and aborts with an internal compiler error "
    )
    probe = "Z9Q7K2lexicalboundprobe"
    dash_run = "|" + "-" * 35 + "|"
    head = (
        f"({phrase.strip()}) " * 6 + " " + probe + " " + dash_run + " "
        + f"({phrase.strip()}) " * 12
    )[:2500]
    assert probe in head and head.find(probe) < 2000
    assert head.find(dash_run) < 2000
    tail = " ".join("--- stderr " + "-" * 70 for _ in range(320))
    return head + tail


def test_lexical_search_survives_pathological_long_query():
    repo_name = f"integration/lexical-long-{uuid4()}"
    repository = PostgresHistoricalIssueRepository()
    phrase = (
        "when building the target triple the linker fails to resolve the codegen "
        "configuration for the selected backend and aborts with an internal compiler error "
    )
    probe = "Z9Q7K2lexicalboundprobe"
    try:
        # body contains every head word so the (ANDed) bounded tsquery still matches
        repository.upsert(
            _issue(
                repo_name,
                1,
                probe,
                phrase.strip() + " " + probe,
            )
        )
        long_query = _pathological_long_query()
        assert len(long_query) > 15000
        hits = repository.lexical_search(repo_name, long_query, 10)
        assert hits, "long query must still return candidates"
        assert {hit.issue_number for hit in hits} == {1}
    finally:
        _cleanup(repo_name)


def test_chunk_search_respects_query_time_and_is_idempotent():
    repo_name = f"integration/chunk-time-{uuid4()}"
    repository = PostgresHistoricalIssueRepository()
    provider = FakeEmbeddingProvider(dimensions=384)
    settings = Settings(
        embedding_provider="fake",
        embedding_model=provider.model_name,
        embedding_dimension=384,
        embedding_chunk_size=48,
        embedding_chunk_overlap=8,
        embedding_max_chunks=4,
    )
    older_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    future_time = datetime(2027, 1, 1, tzinfo=timezone.utc)
    try:
        older = _issue(repo_name, 1, "Terminal loses focus", "focus changes after command")
        newer = _issue(repo_name, 2, "Terminal loses focus", "same symptom in future")
        older.github_created_at = older_time
        newer.github_created_at = future_time
        first = repository.upsert(older)
        second = repository.upsert(newer)
        for stored in (first, second):
            embed_historical_issue_chunks(
                stored.historical_issue_id,
                repository=repository,
                provider=provider,
                settings=settings,
            )
        calls_after_first_index = provider.call_count
        unchanged = embed_historical_issue_chunks(
            first.historical_issue_id,
            repository=repository,
            provider=provider,
            settings=settings,
        )
        assert unchanged["status"] == "unchanged"
        assert provider.call_count == calls_after_first_index

        query_vector = provider.embed_query(["Title: Terminal loses focus\nBody:\nfocus"])
        exact = repository.chunk_vector_search(
            repo_name,
            query_vector,
            provider.model_name,
            10,
            created_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
            exact=True,
        )
        hnsw = repository.chunk_vector_search(
            repo_name,
            query_vector,
            provider.model_name,
            10,
            created_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
            exact=False,
        )
        assert [hit.issue_number for hit in exact] == [1]
        assert [hit.issue_number for hit in hnsw] == [1]
    finally:
        _cleanup(repo_name)
