#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from psycopg.rows import dict_row

from app.core.config import get_settings
from app.db.connection import connect


def main() -> None:
    parser = argparse.ArgumentParser(description="输出真实 RAG 语料与长文本统计")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    repos = settings.evaluation_repositories
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo, count(*) AS issue_count,
                   count(*) FILTER (WHERE embedding IS NOT NULL) AS head_embedding_count,
                   count(*) FILTER (WHERE embedding_original_tokens > 512) AS long_issue_count,
                   count(*) FILTER (WHERE embedding_truncated) AS head_truncated_count,
                   sum(GREATEST(embedding_original_tokens - embedding_embedded_tokens, 0))
                       AS head_lost_tokens,
                   sum(embedding_original_tokens) AS head_original_tokens,
                   avg(chunk_count) AS average_chunk_count,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY chunk_count)
                       AS p95_chunk_count,
                   sum(chunk_truncated_token_count) AS chunk_truncated_tokens,
                   sum(chunk_original_token_count) AS chunk_original_tokens,
                   min(github_created_at) AS earliest_issue_created_at,
                   max(github_created_at) AS latest_issue_created_at
            FROM historical_issues
            WHERE repo = ANY(%s)
            GROUP BY repo ORDER BY repo
            """,
            (repos,),
        )
        rows = list(cur.fetchall())
        cur.execute(
            """
            SELECT pg_size_pretty(pg_relation_size('idx_historical_issues_embedding_hnsw_384'))
                       AS head_hnsw_size,
                   pg_size_pretty(pg_relation_size('idx_historical_issue_chunks_hnsw_384'))
                       AS chunk_hnsw_size,
                   (SELECT count(*) FROM historical_issue_chunks) AS chunk_row_count
            """
        )
        indexes = cur.fetchone()
    per_repo = {}
    for row in rows:
        issue_count = row["issue_count"]
        head_original = row["head_original_tokens"] or 0
        chunk_original = row["chunk_original_tokens"] or 0
        per_repo[row["repo"]] = {
            **{
                key: (
                    value.isoformat()
                    if hasattr(value, "isoformat")
                    else float(value) if isinstance(value, Decimal) else value
                )
                for key, value in row.items()
                if key != "repo"
            },
            "long_issue_ratio": row["long_issue_count"] / issue_count if issue_count else None,
            "head512_lost_token_ratio": (
                (row["head_lost_tokens"] or 0) / head_original if head_original else None
            ),
            "chunk_lost_token_ratio": (
                (row["chunk_truncated_tokens"] or 0) / chunk_original
                if chunk_original else None
            ),
        }
    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "chunk_strategy": {
            "version": settings.embedding_chunk_strategy_version,
            "size": settings.embedding_chunk_size,
            "overlap": settings.embedding_chunk_overlap,
            "max_chunks": settings.embedding_max_chunks,
        },
        "per_repo": per_repo,
        "indexes": dict(indexes),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
