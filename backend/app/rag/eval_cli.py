import argparse
import hashlib
import json
import math
import time
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from app.rag.embedding import FakeEmbeddingProvider
from app.rag.eval_metrics import calculate_duplicate_metrics
from app.rag.eval_schema import DuplicateEvalCase, DuplicateEvalPrediction
from app.rag.retrieval import HybridRetriever
from app.rag.schema import SearchHit


def load_jsonl(path: Path, model):
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class InMemoryEvalRepository:
    def __init__(self, corpus: list[dict], provider: FakeEmbeddingProvider):
        self.corpus = corpus
        self.provider = provider
        self.vectors = {
            (item["repo"], item["issue_number"]): provider.embed(
                [f"{item['title']}\n{item.get('body', '')}"]
            )[0]
            for item in corpus
        }

    def lexical_search(self, repo, query, limit, exclude_issue_number=None):
        hits = []
        for index, item in enumerate(self.corpus, 1):
            if item["repo"] != repo or item["issue_number"] == exclude_issue_number:
                continue
            score = SequenceMatcher(
                None,
                query.lower(),
                f"{item['title']}\n{item.get('body', '')}".lower(),
            ).ratio()
            hits.append(self._hit(index, item, score))
        return sorted(hits, key=lambda hit: -hit.score)[:limit]

    def vector_search(
        self,
        repo,
        query_vector,
        model_name,
        dimensions,
        limit,
        exclude_issue_number=None,
    ):
        hits = []
        for index, item in enumerate(self.corpus, 1):
            if item["repo"] != repo or item["issue_number"] == exclude_issue_number:
                continue
            vector = self.vectors[(item["repo"], item["issue_number"])]
            score = sum(a * b for a, b in zip(query_vector, vector, strict=True)) / (
                math.sqrt(sum(a * a for a in query_vector))
                * math.sqrt(sum(b * b for b in vector))
            )
            hits.append(self._hit(index, item, score))
        return sorted(hits, key=lambda hit: -hit.score)[:limit]

    @staticmethod
    def _hit(index, item, score):
        return SearchHit(
            historical_issue_id=index,
            repo=item["repo"],
            issue_number=item["issue_number"],
            title=item["title"],
            body=item.get("body", ""),
            state=item.get("state", "closed"),
            score=score,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="比较历史 Issue 查重检索模式")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duplicate-threshold", type=float, default=0.015)
    args = parser.parse_args()
    cases = load_jsonl(args.dataset, DuplicateEvalCase)
    corpus = [
        json.loads(line)
        for line in args.corpus.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    provider = FakeEmbeddingProvider()
    repository = InMemoryEvalRepository(corpus, provider)
    retriever = HybridRetriever(repository=repository, embedding_provider=provider)
    metrics = {}
    raw_predictions = {}
    for mode in ("lexical", "vector", "hybrid"):
        predictions = []
        for case in cases:
            started = time.perf_counter()
            result = retriever.search(
                case.query.repo,
                case.query.title,
                case.query.body,
                mode=mode,
                top_k=10,
                exclude_issue_number=case.query.issue_number,
            )
            ranked = [candidate.issue_number for candidate in result.candidates]
            top_score = result.candidates[0].rrf_score if result.candidates else 0.0
            predicted_duplicate = top_score >= args.duplicate_threshold
            predictions.append(
                DuplicateEvalPrediction(
                    case_id=case.id,
                    mode=mode,
                    ranked_issue_numbers=ranked,
                    predicted_is_duplicate=predicted_duplicate,
                    predicted_candidate_issue_number=(
                        ranked[0] if predicted_duplicate and ranked else None
                    ),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    degraded=result.degraded,
                )
            )
        metrics[mode] = calculate_duplicate_metrics(cases, predictions)
        raw_predictions[mode] = [item.model_dump(mode="json") for item in predictions]

    dataset_bytes = args.dataset.read_bytes()
    corpus_bytes = args.corpus.read_bytes()
    report = {
        "schema_version": "1.0",
        "eval_id": str(uuid4()),
        "runner_type": "synthetic_fake_embedding_baseline",
        "publishable_model_score": False,
        "dataset_hash": hashlib.sha256(dataset_bytes).hexdigest(),
        "corpus_hash": hashlib.sha256(corpus_bytes).hexdigest(),
        "embedding_model": provider.model_name,
        "embedding_dimensions": provider.dimensions,
        "duplicate_threshold": args.duplicate_threshold,
        "metrics": metrics,
        "predictions": raw_predictions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
