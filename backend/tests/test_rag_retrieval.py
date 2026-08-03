import pytest

from app.rag.embedding import FakeEmbeddingProvider
from app.rag.retrieval import HybridRetriever, reciprocal_rank_fusion
from app.rag.schema import SearchHit


def _hit(issue_id: int, number: int, score: float) -> SearchHit:
    return SearchHit(
        historical_issue_id=issue_id,
        repo="owner/repo",
        issue_number=number,
        title=f"Issue {number}",
        body=f"Evidence {number}",
        state="closed",
        score=score,
    )


class FakeRepository:
    def __init__(self):
        self.lexical = [_hit(1, 10, 0.9), _hit(2, 20, 0.8)]
        self.vector = [_hit(2, 20, 0.95), _hit(3, 30, 0.7)]
        self.calls = []

    def lexical_search(
        self, repo, query, limit, exclude_issue_number=None, created_before=None
    ):
        self.calls.append(("lexical", repo, exclude_issue_number))
        return self.lexical[:limit]

    def vector_search(
        self,
        repo,
        query_vector,
        model_name,
        dimensions,
        limit,
        exclude_issue_number=None,
        created_before=None,
        *,
        exact=False,
    ):
        self.calls.append(("vector", repo, exclude_issue_number))
        return self.vector[:limit]


class FailingProvider:
    model_name = "failure"
    dimensions = 8
    last_observations = []

    def embed(self, texts):
        raise RuntimeError("embedding unavailable")

    def embed_query(self, texts):
        return self.embed(texts)


def test_rrf_score_and_shared_candidate_ranking():
    candidates = reciprocal_rank_fusion(
        [_hit(1, 10, 0.9), _hit(2, 20, 0.8)],
        [_hit(2, 20, 0.95), _hit(3, 30, 0.7)],
        rrf_k=60,
    )
    assert [candidate.issue_number for candidate in candidates] == [20, 10, 30]
    shared = candidates[0]
    assert shared.rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert shared.sources == ["lexical", "vector"]
    assert shared.lexical_rank == 2
    assert shared.vector_rank == 1


def test_hybrid_search_preserves_repo_isolation_and_order():
    repository = FakeRepository()
    retriever = HybridRetriever(repository, FakeEmbeddingProvider(dimensions=8))
    result = retriever.search(
        "owner/repo", "login crash", "details", exclude_issue_number=99
    )
    assert [candidate.issue_number for candidate in result.candidates] == [20, 10, 30]
    assert repository.calls == [
        ("lexical", "owner/repo", 99),
        ("vector", "owner/repo", 99),
    ]


def test_lexical_and_vector_modes_use_their_own_ranking():
    repository = FakeRepository()
    retriever = HybridRetriever(repository, FakeEmbeddingProvider(dimensions=8))
    lexical = retriever.search("owner/repo", "q", "", mode="lexical")
    vector = retriever.search("owner/repo", "q", "", mode="vector")
    assert [item.issue_number for item in lexical.candidates] == [10, 20]
    assert [item.issue_number for item in vector.candidates] == [20, 30]


def test_embedding_failure_degrades_to_lexical():
    repository = FakeRepository()
    result = HybridRetriever(repository, FailingProvider()).search(
        "owner/repo", "q", "", mode="hybrid"
    )
    assert result.degraded is True
    assert result.degradation_reason == "RuntimeError"
    assert [item.issue_number for item in result.candidates] == [10, 20]
    assert all(item.sources == ["lexical"] for item in result.candidates)


def test_invalid_rrf_k_is_rejected():
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], [], rrf_k=0)
