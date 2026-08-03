from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.rag.benchmark import (
    BenchmarkConfig,
    RetrievalBenchmark,
    compare_exact_hnsw,
    query_scores,
    summarize,
    tune_on_dev,
)


def test_retrieval_metrics_support_multiple_cluster_relevants():
    scores = query_scores([30, 10, 40, 20], {10, 20})
    assert scores["recall_at_1"] == 0
    assert scores["recall_at_5"] == 1
    assert scores["mrr_at_10"] == pytest.approx(0.5)
    assert 0 < scores["ndcg_at_10"] < 1


def test_summary_reports_raw_query_hits_and_bootstrap_ci():
    records = [
        {
            "ranked": [10], "relevant": [10], "latency_ms": 1.0,
            **query_scores([10], {10}),
        },
        {
            "ranked": [30], "relevant": [20], "latency_ms": 3.0,
            **query_scores([30], {20}),
        },
    ]
    report = summarize(records, bootstrap_iterations=20)
    assert report["raw_hits"] == {"at_1": 1, "at_5": 1, "at_10": 1}
    assert report["p50_latency_ms"] == 2.0
    assert report["bootstrap_95_ci"]["recall_at_1"]["iterations"] == 20


def test_hnsw_comparison_is_relative_to_exact_top_k():
    exact = [{"id": "q", "ranked": [1, 2], "latency_ms": 4.0}]
    approx = [{"id": "q", "ranked": [1, 3], "latency_ms": 2.0}]
    result = compare_exact_hnsw(exact, approx, k=2)
    assert result["hnsw_recall_relative_to_exact"] == 0.5
    assert result["top_k_exact_order_match_rate"] == 0


def test_benchmark_runs_all_methods_with_time_constraint_and_tunes_dev():
    class Provider:
        query_prefix = ""

    class Retriever:
        def __init__(self):
            self.calls = []

        def search(self, repo, title, body, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                candidates=[SimpleNamespace(issue_number=10), SimpleNamespace(issue_number=30)],
                degraded=False,
            )

    benchmark = RetrievalBenchmark.__new__(RetrievalBenchmark)
    benchmark.settings = Settings()
    benchmark.provider = Provider()
    benchmark.retriever = Retriever()
    qrels = [{
        "id": "q1",
        "repo": "owner/repo",
        "query_issue_number": 20,
        "query_created_at": "2026-01-01T00:00:00Z",
        "query_title": "Crash",
        "query_body": "details",
        "relevant_issue_numbers": [10],
    }]
    report = benchmark.run(qrels, BenchmarkConfig("", "max_chunk_score"), bootstrap_iterations=20)
    assert set(report["summaries"]) == {
        "lexical", "vector_head512", "vector_chunked",
        "hybrid_head512_rrf", "hybrid_chunked_rrf",
    }
    assert all(call["created_before"] == qrels[0]["query_created_at"] for call in benchmark.retriever.calls)
    tuning = tune_on_dev(benchmark, qrels, bootstrap_iterations=20)
    assert len(tuning["trials"]) == 4
    assert tuning["test_observed"] is False
