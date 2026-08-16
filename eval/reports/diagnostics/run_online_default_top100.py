#!/usr/bin/env python3
"""Resumable Top-100 diagnostic for the actual online retrieval defaults."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.rag.embedding import get_embedding_provider
from app.rag.retrieval import HybridRetriever

from run_top100_diagnostic import atomic_write, make_record, read_jsonl


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-qrels", type=Path, required=True)
    parser.add_argument("--test-qrels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--cooldown-seconds", type=float, default=0.0)
    args = parser.parse_args()
    if args.cooldown_seconds < 0:
        parser.error("cooldown-seconds must be non-negative")

    datasets = {"dev": args.dev_qrels, "test": args.test_qrels}
    hashes = {split: sha256(path) for split, path in datasets.items()}
    settings = get_settings()
    provider = get_embedding_provider()
    configured_prefix = provider.query_prefix
    retriever = HybridRetriever(embedding_provider=provider)

    if args.output.exists():
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        if payload["source_commit"] != args.source_commit:
            parser.error("Refusing to resume an artifact from another commit")
        if payload["dataset_hashes"] != hashes:
            parser.error("Refusing to resume after dataset hash changed")
    else:
        payload = {
            "schema_version": "diagnostic-online-default-top100-v1",
            "diagnostic_only": True,
            "formal_test_status": "post-hoc diagnostic; not a new unseen held-out test",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "source_commit": args.source_commit,
            "dataset_hashes": hashes,
            "max_k": 100,
            "online_default": {
                "mode": "hybrid",
                "vector_strategy": "head512",
                "configured_top_k": settings.duplicate_top_k,
                "diagnostic_top_k": 100,
                "exact": False,
                "ann": "HNSW",
                "query_prefix": configured_prefix,
                "embedding_model": settings.embedding_model,
                "embedding_dimension": settings.embedding_dimension,
                "rrf_k": settings.duplicate_rrf_k,
                "reranker_enabled": settings.duplicate_reranker_enabled,
            },
            "execution": {
                "notes": [
                    "One HybridRetriever.search(top_k=100) call per query.",
                    "A separate real top_k=5 call captures the configured production baseline.",
                    "The Top-100 ranking is sliced for the deeper candidate curve.",
                    "The evaluation time boundary is retained to prevent future-candidate leakage.",
                ],
                "errors": [],
            },
            "runs": {"dev": [], "test": []},
            "configured_top5_runs": {"dev": [], "test": []},
        }
        atomic_write(args.output, payload)

    provider.embed_query(["IssueFlow online default diagnostic warmup"])
    for split, path in datasets.items():
        qrels = read_jsonl(path)
        if args.max_queries is not None:
            qrels = qrels[: args.max_queries]
        records = payload["runs"][split]
        completed = {record["id"] for record in records if "id" in record}
        for position, qrel in enumerate(qrels, 1):
            if qrel["id"] in completed:
                continue
            print(
                json.dumps(
                    {
                        "event": "online_query_start",
                        "split": split,
                        "position": position,
                        "total": len(qrels),
                        "id": qrel["id"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            started = time.perf_counter()
            try:
                result = retriever.search(
                    qrel["repo"],
                    qrel["query_title"],
                    qrel.get("query_body") or "",
                    mode="hybrid",
                    top_k=100,
                    exclude_issue_number=qrel["query_issue_number"],
                    created_before=qrel["query_created_at"],
                    vector_strategy="head512",
                    exact=False,
                )
                latency_ms = (time.perf_counter() - started) * 1000
                record = make_record(qrel, result, latency_ms, provider)
                records.append(record)
                print(
                    json.dumps(
                        {
                            "event": "online_query_done",
                            "split": split,
                            "position": position,
                            "latency_ms": round(latency_ms, 3),
                            "first_relevant_rank": record["first_relevant_rank"],
                            "degraded": record["degraded"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                error = {
                    "id": qrel["id"],
                    "split": split,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                records.append(error)
                payload["execution"]["errors"].append(error)
                print(json.dumps({"event": "online_query_error", **error}, ensure_ascii=False), flush=True)
            atomic_write(args.output, payload)
            if args.cooldown_seconds:
                time.sleep(args.cooldown_seconds)

        # The retriever couples branch fetch depth to top_k (3 * top_k).  A slice
        # of the Top-100 fusion ranking is therefore not guaranteed to equal a
        # real configured Top-5 call.  Run Top-5 once as the production baseline.
        top5_records = payload["configured_top5_runs"][split]
        top5_completed = {record["id"] for record in top5_records if "id" in record}
        for position, qrel in enumerate(qrels, 1):
            if qrel["id"] in top5_completed:
                continue
            print(
                json.dumps(
                    {
                        "event": "configured_top5_start",
                        "split": split,
                        "position": position,
                        "total": len(qrels),
                        "id": qrel["id"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            started = time.perf_counter()
            try:
                result = retriever.search(
                    qrel["repo"],
                    qrel["query_title"],
                    qrel.get("query_body") or "",
                    mode="hybrid",
                    top_k=settings.duplicate_top_k,
                    exclude_issue_number=qrel["query_issue_number"],
                    created_before=qrel["query_created_at"],
                    vector_strategy="head512",
                    exact=False,
                )
                latency_ms = (time.perf_counter() - started) * 1000
                record = make_record(qrel, result, latency_ms, provider)
                top5_records.append(record)
                print(
                    json.dumps(
                        {
                            "event": "configured_top5_done",
                            "split": split,
                            "position": position,
                            "latency_ms": round(latency_ms, 3),
                            "first_relevant_rank": record["first_relevant_rank"],
                            "degraded": record["degraded"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                error = {
                    "id": qrel["id"],
                    "split": split,
                    "run": "configured_top5",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                top5_records.append(error)
                payload["execution"]["errors"].append(error)
                print(json.dumps({"event": "configured_top5_error", **error}, ensure_ascii=False), flush=True)
            atomic_write(args.output, payload)
            if args.cooldown_seconds:
                time.sleep(args.cooldown_seconds)

    complete = all(
        len(payload["runs"][split]) == len(read_jsonl(path))
        and len(payload["configured_top5_runs"][split]) == len(read_jsonl(path))
        for split, path in datasets.items()
    )
    if complete:
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write(args.output, payload)
    print(
        json.dumps(
            {
                "event": "online_run_finished",
                "completed": complete,
                "error_count": len(payload["execution"]["errors"]),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
