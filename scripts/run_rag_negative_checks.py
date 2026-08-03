#!/usr/bin/env python3
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

from app.db.connection import connect
from app.rag.embedding import get_embedding_provider
from app.rag.retrieval import HybridRetriever


def main() -> None:
    parser = argparse.ArgumentParser(description="运行简单负样本与困难负样本定性检查")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--frozen-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = list(csv.DictReader(args.candidates.open(encoding="utf-8", newline="")))
    frozen = json.loads(args.frozen_config.read_text(encoding="utf-8"))["selected"]
    provider = get_embedding_provider()
    if hasattr(provider, "query_prefix"):
        provider.query_prefix = frozen["query_prefix"]
    retriever = HybridRetriever(embedding_provider=provider)
    checks = []
    for row in rows:
        if row["candidate_kind"] not in {"ordinary_negative", "hard_negative"}:
            continue
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT title, body, github_created_at FROM historical_issues
                WHERE repo = %s AND issue_number = %s
                """,
                (row["query_repo"], int(row["query_issue_number"])),
            )
            query = cur.fetchone()
        if query is None:
            continue
        result = retriever.search(
            row["query_repo"], query["title"], query["body"], mode="hybrid",
            top_k=10, exclude_issue_number=int(row["query_issue_number"]),
            created_before=query["github_created_at"], vector_strategy="chunked",
            chunk_aggregation=frozen["chunk_aggregation"], exact=True,
        )
        ranked = [item.issue_number for item in result.candidates]
        proposed = int(row["proposed_duplicate_issue_number"])
        checks.append({
            "candidate_kind": row["candidate_kind"],
            "query_issue_number": int(row["query_issue_number"]),
            "proposed_issue_number": proposed,
            "proposed_candidate_rank": (
                ranked.index(proposed) + 1 if proposed in ranked else None
            ),
            "proposed_candidate_is_top1": bool(ranked and ranked[0] == proposed),
            "human_label": row["human_label"],
            "interpretation": (
                "easy_negative_pair_check"
                if row["candidate_kind"] == "ordinary_negative"
                else "qualitative_only_not_ground_truth"
            ),
        })
    easy = [item for item in checks if item["candidate_kind"] == "ordinary_negative"]
    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "easy_negative_check": {
            "sample_count": len(easy),
            "proposed_pair_top1_false_positive_count": sum(
                item["proposed_candidate_is_top1"] for item in easy
            ),
            "proposed_pair_top1_false_positive_rate": (
                sum(item["proposed_candidate_is_top1"] for item in easy) / len(easy)
                if easy else None
            ),
            "scope": "simple pair check; not hard-negative coverage or classification F1",
        },
        "hard_negative_stress_set": {
            "sample_count": sum(
                item["candidate_kind"] == "hard_negative" for item in checks
            ),
            "ground_truth": False,
            "analysis": "qualitative_only",
        },
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
