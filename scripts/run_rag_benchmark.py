#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.rag.benchmark import (
    BenchmarkConfig,
    RetrievalBenchmark,
    compare_exact_hnsw,
    dataset_hash,
    load_qrels,
    tune_on_dev,
)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行无泄漏、时间安全的查重检索评测")
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--frozen-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-iterations", type=int, default=2000)
    args = parser.parse_args()
    if args.bootstrap_iterations < 2000:
        parser.error("正式报告的 Bootstrap 次数不得少于 2000")
    qrels = load_qrels(args.qrels)
    benchmark = RetrievalBenchmark()
    if args.split == "dev":
        tuning = tune_on_dev(
            benchmark, qrels, bootstrap_iterations=args.bootstrap_iterations
        )
        _write(args.frozen_config, tuning)
    else:
        if not args.frozen_config.exists():
            parser.error("运行 test 前必须存在由 dev 生成的冻结配置")
        tuning = json.loads(args.frozen_config.read_text(encoding="utf-8"))
        if tuning.get("test_observed"):
            parser.error("冻结配置已标记 test_observed，禁止反复查看 test 调参")
    selected = tuning["selected"]
    config = BenchmarkConfig(
        selected["query_prefix"], selected["chunk_aggregation"], exact=True
    )
    exact = benchmark.run(
        qrels, config, bootstrap_iterations=args.bootstrap_iterations
    )
    approximate = benchmark.run(
        qrels,
        BenchmarkConfig(config.query_prefix, config.chunk_aggregation, exact=False),
        methods=["vector_head512", "vector_chunked"],
        bootstrap_iterations=args.bootstrap_iterations,
    )
    comparison = {
        method: compare_exact_hnsw(
            exact["predictions"][method], approximate["predictions"][method]
        )
        for method in ("vector_head512", "vector_chunked")
    }
    report = {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "split": args.split,
        "dataset_hash": dataset_hash(args.qrels),
        "publishable_retrieval_evaluation": args.split == "test",
        "retrieval_input_fields": ["title", "body"],
        "candidate_rule": "same repo and created_at < query_created_at and not self",
        "selected_dev_config": selected,
        "metrics": exact["summaries"],
        "exact_vs_hnsw": comparison,
        "predictions": exact["predictions"],
    }
    _write(args.output, report)
    if args.split == "test":
        tuning["test_observed"] = True
        tuning["test_dataset_hash"] = report["dataset_hash"]
        _write(args.frozen_config, tuning)
    print(json.dumps({
        "split": args.split,
        "query_count": len(qrels),
        "selected": selected,
        "exact_vs_hnsw": comparison,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
