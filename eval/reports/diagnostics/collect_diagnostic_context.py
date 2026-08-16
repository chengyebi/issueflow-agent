#!/usr/bin/env python3
"""Collect non-secret database/environment facts for the retrieval diagnostic."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from psycopg.rows import dict_row

from app.core.config import get_settings
from app.db.connection import connect


REPOS = ("microsoft/vscode", "nodejs/node", "rust-lang/rust")


def clean(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT version() AS version")
        postgres_version = cur.fetchone()["version"]
        cur.execute(
            "SELECT extname, extversion FROM pg_extension "
            "WHERE extname IN ('vector','pg_trgm') ORDER BY extname"
        )
        extensions = list(cur.fetchall())
        cur.execute("SELECT version_num FROM alembic_version")
        alembic_version = cur.fetchone()["version_num"]
        cur.execute(
            """
            SELECT repo,
                   count(*) AS historical_issue_count,
                   count(*) FILTER (WHERE embedding IS NOT NULL) AS with_head_embedding,
                   count(*) FILTER (WHERE embedding IS NULL) AS missing_head_embedding,
                   count(*) FILTER (WHERE chunk_count > 0) AS with_chunk_metadata,
                   count(*) FILTER (WHERE COALESCE(chunk_count, 0) = 0)
                       AS missing_chunk_metadata,
                   sum(COALESCE(chunk_count, 0)) AS metadata_chunk_count,
                   count(*) FILTER (WHERE embedding_truncated) AS head_truncated_count,
                   count(*) FILTER (WHERE chunk_truncated_token_count > 0)
                       AS chunk_truncated_count,
                   sum(COALESCE(chunk_truncated_token_count, 0))
                       AS chunk_truncated_tokens,
                   avg(chunk_count) AS average_chunk_count,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY chunk_count)
                       AS p95_chunk_count,
                   min(github_created_at) AS earliest_created_at,
                   max(github_created_at) AS latest_created_at
            FROM historical_issues
            WHERE repo = ANY(%s)
            GROUP BY repo ORDER BY repo
            """,
            (list(REPOS),),
        )
        corpus = list(cur.fetchall())
        cur.execute(
            """
            SELECT hi.repo, count(*) AS actual_chunk_count,
                   count(DISTINCT hic.historical_issue_id) AS issues_with_actual_chunks
            FROM historical_issue_chunks hic
            JOIN historical_issues hi ON hi.id = hic.historical_issue_id
            WHERE hi.repo = ANY(%s)
            GROUP BY hi.repo ORDER BY hi.repo
            """,
            (list(REPOS),),
        )
        actual_chunks = {row["repo"]: row for row in cur.fetchall()}
        cur.execute(
            """
            SELECT c.relname AS index_name, i.indisvalid, i.indisready,
                   pg_relation_size(c.oid) AS size_bytes,
                   pg_size_pretty(pg_relation_size(c.oid)) AS size_pretty
            FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname IN (
                'idx_historical_issues_embedding_hnsw_384',
                'idx_historical_issue_chunks_hnsw_384'
            ) ORDER BY c.relname
            """
        )
        indexes = list(cur.fetchall())
        cur.execute(
            """
            SELECT embedding_model, embedding_dimensions, embedding_text_version,
                   count(*) AS issue_count
            FROM historical_issues WHERE embedding IS NOT NULL
            GROUP BY 1,2,3 ORDER BY 4 DESC
            """
        )
        head_versions = list(cur.fetchall())
        cur.execute(
            """
            SELECT chunk_embedding_model, chunk_strategy_version, tokenizer_name,
                   chunk_size, chunk_overlap, count(*) AS issue_count
            FROM historical_issues WHERE chunk_count > 0
            GROUP BY 1,2,3,4,5 ORDER BY 6 DESC
            """
        )
        chunk_versions = list(cur.fetchall())
        cur.execute(
            """
            SELECT count(*) AS mismatch_count
            FROM historical_issues hi
            WHERE COALESCE(hi.chunk_count, 0) <>
                  (SELECT count(*) FROM historical_issue_chunks hic
                   WHERE hic.historical_issue_id = hi.id)
            """
        )
        chunk_count_mismatches = cur.fetchone()["mismatch_count"]
        cur.execute(
            """
            SELECT repo, issue_number, title, github_created_at,
                   length(body) AS body_characters,
                   embedding_original_tokens,
                   embedding_embedded_tokens,
                   embedding_truncated,
                   chunk_count,
                   chunk_truncated_token_count
            FROM historical_issues
            WHERE repo = ANY(%s)
            ORDER BY repo, issue_number
            """,
            (list(REPOS),),
        )
        catalog = list(cur.fetchall())

    by_repo = {}
    for row in corpus:
        repo = row["repo"]
        merged = dict(row)
        merged.update(actual_chunks.get(repo, {}))
        merged.pop("repo", None)
        by_repo[repo] = merged
    output = {
        "schema_version": "diagnostic-context-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "postgres_version": postgres_version,
        "extensions": extensions,
        "alembic_version": alembic_version,
        "settings": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "embedding_query_prefix": settings.embedding_query_prefix,
            "embedding_local_files_only": settings.embedding_local_files_only,
            "chunk_size": settings.embedding_chunk_size,
            "chunk_overlap": settings.embedding_chunk_overlap,
            "max_chunks": settings.embedding_max_chunks,
            "chunk_aggregation": settings.embedding_chunk_aggregation,
            "duplicate_top_k": settings.duplicate_top_k,
            "duplicate_rrf_k": settings.duplicate_rrf_k,
            "reranker_enabled": settings.duplicate_reranker_enabled,
            "evaluation_repositories": settings.evaluation_repositories,
        },
        "corpus": by_repo,
        "indexes": indexes,
        "head_embedding_versions": head_versions,
        "chunk_versions": chunk_versions,
        "chunk_count_mismatches": chunk_count_mismatches,
        "issue_catalog": catalog,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(clean(output), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "repos": {repo: values["historical_issue_count"] for repo, values in by_repo.items()},
                "catalog_count": len(catalog),
                "chunk_count_mismatches": chunk_count_mismatches,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
