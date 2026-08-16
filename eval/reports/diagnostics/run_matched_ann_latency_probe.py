#!/usr/bin/env python3
"""Matched thermal-safe Exact/HNSW latency probe on a stratified query sample."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.rag.embedding import get_embedding_provider
from app.rag.retrieval import HybridRetriever

from run_top100_diagnostic import atomic_write, make_record, read_jsonl


REPOS = ("microsoft/vscode", "nodejs/node", "rust-lang/rust")
METHODS = {
    "vector_head512": "head512",
    "vector_chunked": "chunked",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_stratified(qrels: list[dict], per_repo: int) -> list[dict]:
    selected = []
    for repo in REPOS:
        rows = sorted(
            [qrel for qrel in qrels if qrel["repo"] == repo],
            key=lambda qrel: (len(qrel.get("query_body") or ""), qrel["id"]),
        )
        if per_repo >= len(rows):
            selected.extend(rows)
            continue
        indices = [round(index * (len(rows) - 1) / (per_repo - 1)) for index in range(per_repo)]
        selected.extend(rows[index] for index in indices)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-qrels", type=Path, required=True)
    parser.add_argument("--test-qrels", type=Path, required=True)
    parser.add_argument("--frozen-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--per-repo", type=int, default=5)
    parser.add_argument("--cooldown-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.per_repo < 2:
        parser.error("per-repo must be at least 2")
    if args.cooldown_seconds < 0:
        parser.error("cooldown-seconds must be non-negative")

    datasets = {"dev": args.dev_qrels, "test": args.test_qrels}
    hashes = {split: sha256(path) for split, path in datasets.items()}
    frozen = json.loads(args.frozen_config.read_text(encoding="utf-8"))["selected"]
    if args.output.exists():
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        if payload["source_commit"] != args.source_commit or payload["dataset_hashes"] != hashes:
            parser.error("Refusing to resume after commit/dataset change")
    else:
        payload = {
            "schema_version": "diagnostic-matched-ann-latency-v1",
            "diagnostic_only": True,
            "source_commit": args.source_commit,
            "dataset_hashes": hashes,
            "frozen_config": frozen,
            "sample": {
                "per_repo_per_split": args.per_repo,
                "selection": "body-length stratified including endpoints",
                "query_count": args.per_repo * len(REPOS) * len(datasets),
            },
            "execution": {
                "top_k": 100,
                "exact_hnsw_order": "alternated by sample/method index",
                "cooldown_seconds": args.cooldown_seconds,
                "errors": [],
            },
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "runs": {
                split: {
                    method: {"exact": [], "hnsw": []} for method in METHODS
                }
                for split in datasets
            },
        }
        atomic_write(args.output, payload)

    provider = get_embedding_provider()
    provider.query_prefix = frozen["query_prefix"]
    provider.embed_query(["IssueFlow matched ANN latency warmup"])
    retriever = HybridRetriever(embedding_provider=provider)
    for split, path in datasets.items():
        sample = select_stratified(read_jsonl(path), args.per_repo)
        for sample_index, qrel in enumerate(sample):
            for method_index, (method, strategy) in enumerate(METHODS.items()):
                order = ("exact", "hnsw") if (sample_index + method_index) % 2 == 0 else ("hnsw", "exact")
                for search_type in order:
                    records = payload["runs"][split][method][search_type]
                    if qrel["id"] in {record["id"] for record in records if "id" in record}:
                        continue
                    print(json.dumps({
                        "event": "ann_probe_start", "split": split, "method": method,
                        "search_type": search_type, "id": qrel["id"],
                    }, ensure_ascii=False), flush=True)
                    started = time.perf_counter()
                    try:
                        result = retriever.search(
                            qrel["repo"], qrel["query_title"], qrel.get("query_body") or "",
                            mode="vector", top_k=100,
                            exclude_issue_number=qrel["query_issue_number"],
                            created_before=qrel["query_created_at"],
                            vector_strategy=strategy,
                            chunk_aggregation=frozen["chunk_aggregation"],
                            exact=search_type == "exact",
                        )
                        latency_ms = (time.perf_counter() - started) * 1000
                        record = make_record(qrel, result, latency_ms, provider)
                        records.append(record)
                        print(json.dumps({
                            "event": "ann_probe_done", "split": split, "method": method,
                            "search_type": search_type, "id": qrel["id"],
                            "latency_ms": round(latency_ms, 3),
                        }, ensure_ascii=False), flush=True)
                    except Exception as exc:
                        error = {
                            "id": qrel["id"], "split": split, "method": method,
                            "search_type": search_type, "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        }
                        records.append(error)
                        payload["execution"]["errors"].append(error)
                        print(json.dumps({"event": "ann_probe_error", **error}, ensure_ascii=False), flush=True)
                    atomic_write(args.output, payload)
                    if args.cooldown_seconds:
                        time.sleep(args.cooldown_seconds)

    expected = args.per_repo * len(REPOS)
    complete = all(
        len(payload["runs"][split][method][search_type]) == expected
        for split in datasets for method in METHODS for search_type in ("exact", "hnsw")
    )
    if complete:
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write(args.output, payload)
    print(json.dumps({
        "event": "ann_probe_finished", "completed": complete,
        "error_count": len(payload["execution"]["errors"]),
    }), flush=True)


if __name__ == "__main__":
    main()
