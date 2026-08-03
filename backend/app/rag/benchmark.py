import hashlib
import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.rag.embedding import EmbeddingProvider, get_embedding_provider
from app.rag.retrieval import HybridRetriever

METHODS = {
    "lexical": ("lexical", "head512"),
    "vector_head512": ("vector", "head512"),
    "vector_chunked": ("vector", "chunked"),
    "hybrid_head512_rrf": ("hybrid", "head512"),
    "hybrid_chunked_rrf": ("hybrid", "chunked"),
}
BGE_RETRIEVAL_PREFIX = "Represent this sentence for searching relevant passages: "


def load_qrels(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def query_scores(ranked: list[int], relevant: set[int]) -> dict[str, float]:
    def recall(k: int) -> float:
        return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0

    first = next((rank for rank, item in enumerate(ranked[:10], 1) if item in relevant), None)
    dcg = sum(
        (1.0 / math.log2(rank + 1))
        for rank, item in enumerate(ranked[:10], 1)
        if item in relevant
    )
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(10, len(relevant)) + 1))
    return {
        "recall_at_1": recall(1),
        "recall_at_5": recall(5),
        "recall_at_10": recall(10),
        "mrr_at_10": 1.0 / first if first else 0.0,
        "ndcg_at_10": dcg / ideal if ideal else 0.0,
    }


def _bootstrap(records: list[dict], iterations: int, seed: int = 20270803) -> dict:
    rng = random.Random(seed)
    keys = ["recall_at_1", "recall_at_5", "recall_at_10", "mrr_at_10", "ndcg_at_10"]
    samples = {key: [] for key in keys}
    for _ in range(iterations):
        draw = [rng.choice(records) for _ in records]
        for key in keys:
            samples[key].append(statistics.fmean(item[key] for item in draw))
    return {
        key: {
            "low": _percentile(values, 0.025),
            "high": _percentile(values, 0.975),
            "iterations": iterations,
        }
        for key, values in samples.items()
    }


def _bootstrap_macro(records: list[dict], iterations: int, seed: int = 20270803) -> dict:
    rng = random.Random(seed)
    keys = ["recall_at_1", "recall_at_5", "recall_at_10", "mrr_at_10", "ndcg_at_10"]
    grouped = {
        repo: [item for item in records if item["repo"] == repo]
        for repo in sorted({item["repo"] for item in records})
    }
    samples = {key: [] for key in keys}
    for _ in range(iterations):
        repo_means = {key: [] for key in keys}
        for repo_records in grouped.values():
            draw = [rng.choice(repo_records) for _ in repo_records]
            for key in keys:
                repo_means[key].append(statistics.fmean(item[key] for item in draw))
        for key in keys:
            samples[key].append(statistics.fmean(repo_means[key]))
    return {
        key: {
            "low": _percentile(values, 0.025),
            "high": _percentile(values, 0.975),
            "iterations": iterations,
        }
        for key, values in samples.items()
    }


def summarize(records: list[dict], *, bootstrap_iterations: int) -> dict:
    metric_keys = ["recall_at_1", "recall_at_5", "recall_at_10", "mrr_at_10", "ndcg_at_10"]
    summary = {
        "query_count": len(records),
        **{
            key: statistics.fmean(item[key] for item in records) if records else None
            for key in metric_keys
        },
        "raw_hits": {
            f"at_{k}": sum(bool(set(item["ranked"][:k]) & set(item["relevant"])) for item in records)
            for k in (1, 5, 10)
        },
        "p50_latency_ms": _percentile([item["latency_ms"] for item in records], 0.5),
        "p95_latency_ms": _percentile([item["latency_ms"] for item in records], 0.95),
    }
    summary["bootstrap_95_ci"] = (
        _bootstrap(records, bootstrap_iterations) if records else {}
    )
    return summary


@dataclass(frozen=True)
class BenchmarkConfig:
    query_prefix: str
    chunk_aggregation: str
    exact: bool = True


class RetrievalBenchmark:
    def __init__(
        self,
        *,
        provider: EmbeddingProvider | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.provider = provider or get_embedding_provider()
        self.retriever = HybridRetriever(embedding_provider=self.provider)

    def run(
        self,
        qrels: list[dict],
        config: BenchmarkConfig,
        *,
        methods: list[str] | None = None,
        bootstrap_iterations: int = 2000,
    ) -> dict:
        if hasattr(self.provider, "query_prefix"):
            self.provider.query_prefix = config.query_prefix
        methods = methods or list(METHODS)
        raw: dict[str, list[dict]] = {method: [] for method in methods}
        for qrel in qrels:
            relevant = set(qrel["relevant_issue_numbers"])
            for method in methods:
                mode, vector_strategy = METHODS[method]
                started = time.perf_counter()
                result = self.retriever.search(
                    qrel["repo"], qrel["query_title"], qrel.get("query_body") or "",
                    mode=mode, top_k=10,
                    exclude_issue_number=qrel["query_issue_number"],
                    created_before=qrel["query_created_at"],
                    vector_strategy=vector_strategy,
                    chunk_aggregation=config.chunk_aggregation,
                    exact=config.exact,
                )
                latency = (time.perf_counter() - started) * 1000
                ranked = [item.issue_number for item in result.candidates]
                raw[method].append({
                    "id": qrel["id"], "repo": qrel["repo"],
                    "ranked": ranked, "relevant": sorted(relevant),
                    "latency_ms": latency, "degraded": result.degraded,
                    **query_scores(ranked, relevant),
                })
        summaries = {}
        for method, records in raw.items():
            repos = sorted({item["repo"] for item in records})
            per_repo = {
                repo: summarize(
                    [item for item in records if item["repo"] == repo],
                    bootstrap_iterations=bootstrap_iterations,
                )
                for repo in repos
            }
            macro_keys = ["recall_at_1", "recall_at_5", "recall_at_10", "mrr_at_10", "ndcg_at_10"]
            macro = {
                key: statistics.fmean(
                    value[key] for value in per_repo.values() if value[key] is not None
                )
                for key in macro_keys
            }
            macro["repo_count"] = len(per_repo)
            macro["query_count"] = len(records)
            macro["raw_hits"] = {
                key: sum(value["raw_hits"][key] for value in per_repo.values())
                for key in ("at_1", "at_5", "at_10")
            }
            macro["bootstrap_95_ci"] = _bootstrap_macro(
                records, bootstrap_iterations
            ) if records else {}
            summaries[method] = {
                "overall": summarize(records, bootstrap_iterations=bootstrap_iterations),
                "per_repo": per_repo,
                "macro_average": macro,
            }
        return {"summaries": summaries, "predictions": raw}


def tune_on_dev(
    benchmark: RetrievalBenchmark,
    dev_qrels: list[dict],
    *,
    bootstrap_iterations: int = 2000,
) -> dict:
    trials = []
    for prefix_name, prefix in (("no_prefix", ""), ("bge_retrieval", BGE_RETRIEVAL_PREFIX)):
        for aggregation in ("max_chunk_score", "mean_top2_chunk_score"):
            config = BenchmarkConfig(prefix, aggregation, exact=True)
            report = benchmark.run(
                dev_qrels, config,
                methods=["hybrid_chunked_rrf"],
                bootstrap_iterations=bootstrap_iterations,
            )
            score = report["summaries"]["hybrid_chunked_rrf"]["macro_average"]["ndcg_at_10"]
            trials.append({
                "prefix_name": prefix_name,
                "query_prefix": prefix,
                "chunk_aggregation": aggregation,
                "selection_metric": "macro_ndcg_at_10",
                "selection_score": score,
            })
    selected = sorted(
        trials,
        key=lambda item: (
            -item["selection_score"], item["prefix_name"] != "no_prefix",
            item["chunk_aggregation"] != "max_chunk_score",
        ),
    )[0]
    return {"selected": selected, "trials": trials, "test_observed": False}


def compare_exact_hnsw(
    exact_predictions: list[dict], approximate_predictions: list[dict], *, k: int = 10
) -> dict:
    by_id = {item["id"]: item for item in approximate_predictions}
    recalls = []
    exact_match = 0
    for exact in exact_predictions:
        approx = by_id[exact["id"]]
        exact_top = exact["ranked"][:k]
        approx_top = approx["ranked"][:k]
        recalls.append(len(set(exact_top) & set(approx_top)) / len(exact_top) if exact_top else 1.0)
        exact_match += exact_top == approx_top
    return {
        "k": k,
        "hnsw_recall_relative_to_exact": statistics.fmean(recalls) if recalls else None,
        "top_k_exact_order_match_rate": exact_match / len(recalls) if recalls else None,
        "exact_p50_latency_ms": _percentile([x["latency_ms"] for x in exact_predictions], 0.5),
        "hnsw_p50_latency_ms": _percentile([x["latency_ms"] for x in approximate_predictions], 0.5),
        "exact_p95_latency_ms": _percentile([x["latency_ms"] for x in exact_predictions], 0.95),
        "hnsw_p95_latency_ms": _percentile([x["latency_ms"] for x in approximate_predictions], 0.95),
    }


def dataset_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
