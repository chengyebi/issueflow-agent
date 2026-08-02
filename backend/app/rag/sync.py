from collections.abc import Callable, Iterable
from datetime import datetime

from psycopg.rows import dict_row

from app.core.sanitization import sanitize_error_message
from app.db.connection import connect
from app.rag.embedding import EmbeddingProvider
from app.rag.indexing import embed_historical_issue
from app.rag.repository import PostgresHistoricalIssueRepository
from app.rag.schema import HistoricalIssue
from app.services.github import list_repository_issues


def _label_names(raw_labels: list) -> list[str]:
    names = []
    for label in raw_labels or []:
        if isinstance(label, str) and label:
            names.append(label)
        elif isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
    return names


def github_issue_to_historical(repo: str, payload: dict) -> HistoricalIssue:
    return HistoricalIssue(
        repo=repo,
        issue_number=payload["number"],
        title=payload.get("title") or "",
        body=payload.get("body") or "",
        labels=_label_names(payload.get("labels", [])),
        state=payload.get("state", "open"),
        github_updated_at=datetime.fromisoformat(
            payload["updated_at"].replace("Z", "+00:00")
        ),
    )


def sync_repository_issues(
    repo: str,
    *,
    fetcher: Callable[..., Iterable[dict]] = list_repository_issues,
    repository: PostgresHistoricalIssueRepository | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> dict:
    repository = repository or PostgresHistoricalIssueRepository()
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO issue_sync_runs (repo, status) VALUES (%s, 'running') RETURNING id",
            (repo,),
        )
        sync_run_id = cur.fetchone()["id"]

    counters = {
        "scanned_count": 0,
        "upserted_count": 0,
        "unchanged_count": 0,
        "embedded_count": 0,
        "skipped_pull_request_count": 0,
    }
    try:
        for payload in fetcher(repo, state="all", per_page=100):
            counters["scanned_count"] += 1
            if payload.get("pull_request") is not None:
                counters["skipped_pull_request_count"] += 1
                continue
            issue = github_issue_to_historical(repo, payload)
            result = repository.upsert(issue)
            if result.created or result.content_changed:
                counters["upserted_count"] += 1
            else:
                counters["unchanged_count"] += 1
            if embedding_provider is not None:
                outcome = embed_historical_issue(
                    result.historical_issue_id,
                    repository=repository,
                    provider=embedding_provider,
                )
                if outcome["status"] == "embedded":
                    counters["embedded_count"] += 1
    except Exception as exc:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE issue_sync_runs
                SET status = 'failed', error_type = %s, error_message = %s,
                    scanned_count = %s, upserted_count = %s,
                    unchanged_count = %s, embedded_count = %s,
                    skipped_pull_request_count = %s, finished_at = NOW()
                WHERE id = %s
                """,
                (
                    type(exc).__name__,
                    sanitize_error_message(exc),
                    counters["scanned_count"],
                    counters["upserted_count"],
                    counters["unchanged_count"],
                    counters["embedded_count"],
                    counters["skipped_pull_request_count"],
                    sync_run_id,
                ),
            )
        raise

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE issue_sync_runs
            SET status = 'completed', scanned_count = %s, upserted_count = %s,
                unchanged_count = %s, embedded_count = %s,
                skipped_pull_request_count = %s, finished_at = NOW()
            WHERE id = %s
            """,
            (
                counters["scanned_count"],
                counters["upserted_count"],
                counters["unchanged_count"],
                counters["embedded_count"],
                counters["skipped_pull_request_count"],
                sync_run_id,
            ),
        )
    return {"sync_run_id": sync_run_id, "repo": repo, **counters}
