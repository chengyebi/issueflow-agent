import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.connection import connect
from app.rag.embedding import FakeEmbeddingProvider
from app.rag.indexing import embed_historical_issue
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
            "updated_at": "2026-08-01T00:00:00Z",
        },
        {
            "number": 2,
            "title": "PR",
            "body": "code change",
            "state": "open",
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
