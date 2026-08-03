from datetime import datetime
from typing import Protocol

from app.core.config import get_settings
from app.rag.chunking import chunk_query_text
from app.rag.embedding import EmbeddingProvider, get_embedding_provider
from app.rag.repository import PostgresHistoricalIssueRepository
from app.rag.schema import RetrievalResult, SearchHit, SimilarIssueCandidate
from app.rag.text import issue_embedding_text


class SearchRepository(Protocol):
    def lexical_search(
        self, repo: str, query: str, limit: int, exclude_issue_number: int | None = None,
        created_before: datetime | None = None,
    ) -> list[SearchHit]: ...

    def vector_search(
        self,
        repo: str,
        query_vector: list[float],
        model_name: str,
        dimensions: int,
        limit: int,
        exclude_issue_number: int | None = None,
        created_before: datetime | None = None,
        *,
        exact: bool = False,
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
        self.embedding_provider = embedding_provider
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
        labels: list[str] | None = None,
        created_before: datetime | None = None,
        vector_strategy: str = "head512",
        chunk_aggregation: str | None = None,
        exact: bool = False,
    ) -> RetrievalResult:
        if mode not in {"lexical", "vector", "hybrid"}:
            raise ValueError("检索模式必须是 lexical、vector 或 hybrid")
        settings = get_settings()
        if vector_strategy not in {"head512", "chunked"}:
            raise ValueError("vector_strategy 必须是 head512 或 chunked")
        top_k = top_k or settings.duplicate_top_k
        # Labels are deliberately ignored. They can reflect post-resolution
        # maintainer decisions and are not available at business ingress time.
        lexical_query = "\n".join([title, body]).strip()
        embedding_query = issue_embedding_text(title, body)
        fetch_limit = max(top_k * 3, top_k)
        lexical_hits = self.repository.lexical_search(
            repo, lexical_query, fetch_limit, exclude_issue_number, created_before
        )
        vector_hits: list[SearchHit] = []
        query_chunked = None
        degraded = False
        reason = None
        if mode in {"vector", "hybrid"}:
            try:
                provider = self.embedding_provider or get_embedding_provider()
                if vector_strategy == "chunked":
                    chunked = chunk_query_text(
                        title,
                        body,
                        provider,
                        chunk_size=settings.embedding_chunk_size,
                        overlap=settings.embedding_chunk_overlap,
                        max_chunks=settings.embedding_max_chunks,
                    )
                    query_chunked = chunked
                    query_vectors = provider.embed_query(
                        [chunk.text for chunk in chunked.chunks]
                    )
                    vector_hits = self.repository.chunk_vector_search(
                        repo,
                        query_vectors,
                        provider.model_name,
                        fetch_limit,
                        exclude_issue_number,
                        created_before,
                        aggregation=(
                            chunk_aggregation or settings.embedding_chunk_aggregation
                        ),
                        exact=exact,
                    )
                else:
                    query_vector = provider.embed_query([embedding_query])[0]
                    vector_hits = self.repository.vector_search(
                        repo,
                        query_vector,
                        provider.model_name,
                        provider.dimensions,
                        fetch_limit,
                        exclude_issue_number,
                        created_before,
                        exact=exact,
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
            candidates = self.reranker.rerank(embedding_query, candidates)
        provider = self.embedding_provider
        if mode in {"vector", "hybrid"} and provider is None:
            try:
                provider = get_embedding_provider()
            except Exception:
                provider = None
        observations = getattr(provider, "last_observations", []) if provider else []
        observation = observations[0] if observations else None
        return RetrievalResult(
            mode="lexical" if mode == "vector" and degraded else mode,
            degraded=degraded,
            degradation_reason=reason,
            embedding_model=(
                provider.model_name
                if provider and mode in {"vector", "hybrid"} and not degraded
                else None
            ),
            query_original_tokens=(
                query_chunked.original_token_count
                if query_chunked else observation.original_tokens if observation else None
            ),
            query_embedded_tokens=(
                query_chunked.stored_token_count
                if query_chunked else observation.embedded_tokens if observation else None
            ),
            query_truncated=(
                query_chunked.truncated_token_count > 0
                if query_chunked else observation.truncated if observation else None
            ),
            query_chunk_count=(len(query_chunked.chunks) if query_chunked else None),
            query_truncated_tokens=(
                query_chunked.truncated_token_count if query_chunked else None
            ),
            candidates=candidates[:top_k],
        )
