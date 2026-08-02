from typing import Protocol

from app.core.config import get_settings
from app.rag.embedding import EmbeddingProvider, create_embedding_provider
from app.rag.repository import PostgresHistoricalIssueRepository
from app.rag.schema import RetrievalResult, SearchHit, SimilarIssueCandidate


class SearchRepository(Protocol):
    def lexical_search(
        self, repo: str, query: str, limit: int, exclude_issue_number: int | None = None
    ) -> list[SearchHit]: ...

    def vector_search(
        self,
        repo: str,
        query_vector: list[float],
        model_name: str,
        dimensions: int,
        limit: int,
        exclude_issue_number: int | None = None,
    ) -> list[SearchHit]: ...


class Reranker(Protocol):
    def rerank(
        self, query: str, candidates: list[SimilarIssueCandidate]
    ) -> list[SimilarIssueCandidate]: ...


def reciprocal_rank_fusion(
    lexical_hits: list[SearchHit],
    vector_hits: list[SearchHit],
    rrf_k: int = 60,
) -> list[SimilarIssueCandidate]:
    if rrf_k <= 0:
        raise ValueError("RRF k 必须大于 0")
    combined: dict[int, dict] = {}
    for source, hits in (("lexical", lexical_hits), ("vector", vector_hits)):
        for rank, hit in enumerate(hits, 1):
            item = combined.setdefault(
                hit.historical_issue_id,
                {
                    "hit": hit,
                    "rrf_score": 0.0,
                    "sources": [],
                    "lexical_score": None,
                    "vector_score": None,
                    "lexical_rank": None,
                    "vector_rank": None,
                },
            )
            item["rrf_score"] += 1.0 / (rrf_k + rank)
            item["sources"].append(source)
            item[f"{source}_score"] = hit.score
            item[f"{source}_rank"] = rank

    candidates = []
    for item in combined.values():
        hit = item["hit"]
        evidence_text = (hit.body or "").strip().replace("\n", " ")[:240]
        candidates.append(
            SimilarIssueCandidate(
                historical_issue_id=hit.historical_issue_id,
                repo=hit.repo,
                issue_number=hit.issue_number,
                title=hit.title,
                state=hit.state,
                lexical_score=item["lexical_score"],
                vector_score=item["vector_score"],
                rrf_score=item["rrf_score"],
                lexical_rank=item["lexical_rank"],
                vector_rank=item["vector_rank"],
                sources=item["sources"],
                evidence=evidence_text,
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (-candidate.rrf_score, candidate.issue_number),
    )


class HybridRetriever:
    def __init__(
        self,
        repository: SearchRepository | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
    ):
        self.repository = repository or PostgresHistoricalIssueRepository()
        self.embedding_provider = embedding_provider or create_embedding_provider()
        self.reranker = reranker

    def search(
        self,
        repo: str,
        title: str,
        body: str,
        *,
        mode: str = "hybrid",
        top_k: int | None = None,
        exclude_issue_number: int | None = None,
    ) -> RetrievalResult:
        if mode not in {"lexical", "vector", "hybrid"}:
            raise ValueError("检索模式必须是 lexical、vector 或 hybrid")
        settings = get_settings()
        top_k = top_k or settings.duplicate_top_k
        query = f"{title}\n{body}".strip()
        fetch_limit = max(top_k * 3, top_k)
        lexical_hits = self.repository.lexical_search(
            repo, query, fetch_limit, exclude_issue_number
        )
        vector_hits: list[SearchHit] = []
        degraded = False
        reason = None
        if mode in {"vector", "hybrid"}:
            try:
                query_vector = self.embedding_provider.embed([query])[0]
                vector_hits = self.repository.vector_search(
                    repo,
                    query_vector,
                    self.embedding_provider.model_name,
                    self.embedding_provider.dimensions,
                    fetch_limit,
                    exclude_issue_number,
                )
            except Exception as exc:
                degraded = True
                reason = type(exc).__name__

        if mode == "lexical" or (mode == "vector" and degraded):
            vector_hits = []
        elif mode == "vector":
            lexical_hits = []
        candidates = reciprocal_rank_fusion(
            lexical_hits, vector_hits, settings.duplicate_rrf_k
        )
        if self.reranker is not None and settings.duplicate_reranker_enabled:
            candidates = self.reranker.rerank(query, candidates)
        return RetrievalResult(
            mode="lexical" if mode == "vector" and degraded else mode,
            degraded=degraded,
            degradation_reason=reason,
            candidates=candidates[:top_k],
        )
