#!/usr/bin/env python3
"""Isolated, resumable Top-100 retrieval diagnostic.

This script deliberately lives under eval/reports/diagnostics so it cannot alter
the production retrieval defaults or the frozen formal evaluation artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.rag.benchmark import METHODS
from app.rag.embedding import get_embedding_provider
from app.rag.retrieval import HybridRetriever
from app.rag.text import issue_embedding_text


K_VALUES = (1, 5, 10, 20, 30, 50, 100)
VECTOR_METHODS = ("vector_head512", "vector_chunked")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def make_record(qrel: dict, result, latency_ms: float, provider) -> dict:
    ranked = [candidate.issue_number for candidate in result.candidates]
    relevant = sorted(set(qrel["relevant_issue_numbers"]))
    relevant_set = set(relevant)
    first_rank = next(
        (rank for rank, issue_number in enumerate(ranked, 1) if issue_number in relevant_set),
        None,
    )
    query_text = issue_embedding_text(
        qrel["query_title"], qrel.get("query_body") or ""
    )
    return {
        "id": qrel["id"],
        "repo": qrel["repo"],
        "query_issue_number": qrel["query_issue_number"],
        "query_created_at": qrel["query_created_at"],
        "query_title": qrel["query_title"],
        "query_body_characters": len(qrel.get("query_body") or ""),
        "query_token_count": len(provider.tokenize(query_text)),
        "relevant": relevant,
        "ranked": ranked,
        "first_relevant_rank": first_rank,
        "recall": {
            str(k): len(set(ranked[:k]) & relevant_set) / len(relevant_set)
            for k in K_VALUES
        },
        "hit": {
            str(k): bool(set(ranked[:k]) & relevant_set)
            for k in K_VALUES
        },
        "latency_ms": latency_ms,
        "degraded": result.degraded,
        "degradation_reason": result.degradation_reason,
        "query_original_tokens_observed": result.query_original_tokens,
        "query_embedded_tokens_observed": result.query_embedded_tokens,
        "query_truncated": result.query_truncated,
        "query_chunk_count": result.query_chunk_count,
        "query_truncated_tokens": result.query_truncated_tokens,
        "top_candidates": [
            {
                "issue_number": candidate.issue_number,
                "title": candidate.title,
                "lexical_score": candidate.lexical_score,
                "vector_score": candidate.vector_score,
                "rrf_score": candidate.rrf_score,
                "lexical_rank": candidate.lexical_rank,
                "vector_rank": candidate.vector_rank,
                "sources": candidate.sources,
            }
            for candidate in result.candidates[:10]
        ],
    }


def base_payload(args, frozen: dict, datasets: dict[str, Path]) -> dict:
    settings = get_settings()
    return {
        "schema_version": "diagnostic-top100-v1",
        "diagnostic_only": True,
        "formal_test_status": "post-hoc diagnostic; not a new unseen held-out test",
        "started_at": utc_now(),
        "completed_at": None,
        "source_commit": args.source_commit,
        "max_k": args.max_k,
        "k_values": list(K_VALUES),
        "frozen_config": frozen["selected"],
        "dataset_hashes": {split: sha256(path) for split, path in datasets.items()},
        "settings": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "chunk_size": settings.embedding_chunk_size,
            "chunk_overlap": settings.embedding_chunk_overlap,
            "max_chunks": settings.embedding_max_chunks,
            "chunk_aggregation": frozen["selected"]["chunk_aggregation"],
            "duplicate_top_k": settings.duplicate_top_k,
            "duplicate_rrf_k": settings.duplicate_rrf_k,
            "reranker_enabled": settings.duplicate_reranker_enabled,
            "local_files_only": settings.embedding_local_files_only,
        },
        "method_map": {
            method: {"mode": mode, "vector_strategy": strategy}
            for method, (mode, strategy) in METHODS.items()
        },
        "execution": {
            "python": os.sys.version,
            "notes": [
                "One HybridRetriever.search(top_k=100) call per query/method/run mode.",
                "All K metrics are derived from that single Top-100 ranking.",
                "Retriever internally fetches 3 * top_k from each branch before RRF.",
            ],
            "warmup_latency_ms": None,
            "errors": [],
        },
        "runs": {split: {"exact": {}, "hnsw": {}} for split in datasets},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-qrels", type=Path, required=True)
    parser.add_argument("--test-qrels", type=Path, required=True)
    parser.add_argument("--frozen-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--phase", choices=("exact", "hnsw", "both"), default="both")
    parser.add_argument("--split", choices=("dev", "test", "both"), default="both")
    parser.add_argument("--max-k", type=int, default=100)
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--cooldown-seconds", type=float, default=0.0)
    args = parser.parse_args()
    if args.max_k != 100:
        parser.error("This diagnostic is intentionally fixed at max_k=100")
    if args.cooldown_seconds < 0:
        parser.error("cooldown-seconds must be non-negative")

    datasets = {"dev": args.dev_qrels, "test": args.test_qrels}
    frozen = json.loads(args.frozen_config.read_text(encoding="utf-8"))
    if not frozen.get("test_observed"):
        parser.error("Expected the historical TEST to already be observed")
    if args.output.exists():
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        if payload.get("source_commit") != args.source_commit:
            parser.error("Refusing to resume an artifact from another commit")
        if payload.get("dataset_hashes") != {
            split: sha256(path) for split, path in datasets.items()
        }:
            parser.error("Refusing to resume after dataset hash changed")
    else:
        payload = base_payload(args, frozen, datasets)
        atomic_write(args.output, payload)

    provider = get_embedding_provider()
    provider.query_prefix = frozen["selected"]["query_prefix"]
    started = time.perf_counter()
    provider.embed_query(["IssueFlow offline retrieval diagnostic warmup"])
    payload["execution"]["warmup_latency_ms"] = (time.perf_counter() - started) * 1000
    retriever = HybridRetriever(embedding_provider=provider)

    selected_splits = datasets if args.split == "both" else {args.split: datasets[args.split]}
    selected_phases = ("exact", "hnsw") if args.phase == "both" else (args.phase,)
    for split, dataset_path in selected_splits.items():
        qrels = read_jsonl(dataset_path)
        if args.max_queries is not None:
            qrels = qrels[: args.max_queries]
        for phase in selected_phases:
            methods = list(METHODS) if phase == "exact" else list(VECTOR_METHODS)
            for method in methods:
                records = payload["runs"][split][phase].setdefault(method, [])
                completed_ids = {record["id"] for record in records if "id" in record}
                mode, vector_strategy = METHODS[method]
                for position, qrel in enumerate(qrels, 1):
                    if qrel["id"] in completed_ids:
                        continue
                    print(
                        json.dumps(
                            {
                                "event": "query_start",
                                "split": split,
                                "phase": phase,
                                "method": method,
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
                            mode=mode,
                            top_k=args.max_k,
                            exclude_issue_number=qrel["query_issue_number"],
                            created_before=qrel["query_created_at"],
                            vector_strategy=vector_strategy,
                            chunk_aggregation=frozen["selected"]["chunk_aggregation"],
                            exact=phase == "exact",
                        )
                        latency_ms = (time.perf_counter() - started) * 1000
                        record = make_record(qrel, result, latency_ms, provider)
                        records.append(record)
                        print(
                            json.dumps(
                                {
                                    "event": "query_done",
                                    "split": split,
                                    "phase": phase,
                                    "method": method,
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
                            "phase": phase,
                            "method": method,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                            "occurred_at": utc_now(),
                        }
                        records.append(error)
                        payload["execution"]["errors"].append(error)
                        print(json.dumps({"event": "query_error", **error}, ensure_ascii=False), flush=True)
                    atomic_write(args.output, payload)
                    if args.cooldown_seconds:
                        time.sleep(args.cooldown_seconds)

    expected_counts = {
        split: len(read_jsonl(path)) for split, path in datasets.items()
    }
    complete = True
    for split, expected in expected_counts.items():
        for method in METHODS:
            complete &= len(payload["runs"][split]["exact"].get(method, [])) == expected
        for method in VECTOR_METHODS:
            complete &= len(payload["runs"][split]["hnsw"].get(method, [])) == expected
    if complete:
        payload["completed_at"] = utc_now()
    atomic_write(args.output, payload)

    latencies = [
        record["latency_ms"]
        for split_runs in payload["runs"].values()
        for phase_runs in split_runs.values()
        for records in phase_runs.values()
        for record in records
        if "latency_ms" in record
    ]
    print(
        json.dumps(
            {
                "event": "run_finished",
                "completed": complete,
                "completed_at": payload["completed_at"],
                "record_count": len(latencies),
                "p50_latency_ms": percentile(latencies, 0.5),
                "p95_latency_ms": percentile(latencies, 0.95),
                "error_count": len(payload["execution"]["errors"]),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
