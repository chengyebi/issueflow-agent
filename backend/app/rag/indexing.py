from app.core.config import Settings, get_settings
from app.rag.chunking import chunk_issue_text
from app.rag.embedding import EmbeddingProvider, get_embedding_provider
from app.rag.repository import PostgresHistoricalIssueRepository
from app.rag.text import ISSUE_EMBEDDING_TEXT_VERSION, issue_embedding_text


def embed_historical_issue(
    historical_issue_id: int,
    *,
    repository: PostgresHistoricalIssueRepository | None = None,
    provider: EmbeddingProvider | None = None,
) -> dict:
    repository = repository or PostgresHistoricalIssueRepository()
    provider = provider or get_embedding_provider()
    issue = repository.get_for_embedding(historical_issue_id)
    if issue is None:
        raise ValueError(f"历史 Issue 不存在: {historical_issue_id}")
    if (
        issue["embedding_content_hash"] == issue["content_hash"]
        and issue["embedding_model"] == provider.model_name
        and issue["embedding_dimensions"] == provider.dimensions
        and issue.get("embedding_text_version") == ISSUE_EMBEDDING_TEXT_VERSION
    ):
        return {"historical_issue_id": historical_issue_id, "status": "unchanged"}
    text = issue_embedding_text(issue["title"], issue["body"], issue.get("labels"))
    vector = provider.embed([text])[0]
    if len(vector) != provider.dimensions:
        raise ValueError("Embedding Provider 实际向量维度与数据库配置不一致")
    observation = provider.last_observations[0] if provider.last_observations else None
    saved = repository.save_embedding(
        historical_issue_id,
        str(issue["content_hash"]).strip(),
        provider.model_name,
        provider.dimensions,
        vector,
        text_version=ISSUE_EMBEDDING_TEXT_VERSION,
        observation=observation,
    )
    return {
        "historical_issue_id": historical_issue_id,
        "status": "embedded" if saved else "stale",
        "model": provider.model_name,
        "dimensions": provider.dimensions,
        "input_truncated": observation.truncated if observation else None,
        "input_original_tokens": observation.original_tokens if observation else None,
        "input_embedded_tokens": observation.embedded_tokens if observation else None,
    }


def embed_historical_issue_chunks(
    historical_issue_id: int,
    *,
    repository: PostgresHistoricalIssueRepository | None = None,
    provider: EmbeddingProvider | None = None,
    settings: Settings | None = None,
) -> dict:
    repository = repository or PostgresHistoricalIssueRepository()
    provider = provider or get_embedding_provider()
    settings = settings or get_settings()
    if provider.dimensions != 384:
        raise ValueError("historical_issue_chunks 当前要求 384 维 Embedding")
    issue = repository.chunk_state(historical_issue_id)
    if issue is None:
        raise ValueError(f"历史 Issue 不存在: {historical_issue_id}")
    matches = (
        issue.get("chunk_strategy_version") == settings.embedding_chunk_strategy_version
        and issue.get("chunk_embedding_model") == provider.model_name
        and issue.get("tokenizer_name") == provider.model_name
        and issue.get("chunk_size") == settings.embedding_chunk_size
        and issue.get("chunk_overlap") == settings.embedding_chunk_overlap
        and (issue.get("chunk_count") or 0) > 0
    )
    if matches:
        return {"historical_issue_id": historical_issue_id, "status": "unchanged"}

    chunked = chunk_issue_text(
        issue["title"],
        issue["body"],
        provider,
        chunk_size=settings.embedding_chunk_size,
        overlap=settings.embedding_chunk_overlap,
        max_chunks=settings.embedding_max_chunks,
    )
    vectors = provider.embed([chunk.text for chunk in chunked.chunks])
    saved = repository.save_chunks(
        historical_issue_id,
        expected_content_hash=str(issue["content_hash"]).strip(),
        chunks=chunked.chunks,
        vectors=vectors,
        strategy_version=settings.embedding_chunk_strategy_version,
        model_name=provider.model_name,
        tokenizer_name=provider.model_name,
        chunk_size=settings.embedding_chunk_size,
        chunk_overlap=settings.embedding_chunk_overlap,
        original_token_count=chunked.original_token_count,
        stored_token_count=chunked.stored_token_count,
        truncated_token_count=chunked.truncated_token_count,
    )
    return {
        "historical_issue_id": historical_issue_id,
        "status": "embedded" if saved else "stale",
        "chunk_count": len(chunked.chunks),
        "original_token_count": chunked.original_token_count,
        "stored_token_count": chunked.stored_token_count,
        "truncated_token_count": chunked.truncated_token_count,
    }


def embed_repository_issues(
    repo: str,
    *,
    limit: int,
    repository: PostgresHistoricalIssueRepository | None = None,
    provider: EmbeddingProvider | None = None,
    settings: Settings | None = None,
) -> dict:
    """Batch head and chunk embeddings while preserving per-issue idempotency."""
    repository = repository or PostgresHistoricalIssueRepository()
    provider = provider or get_embedding_provider()
    settings = settings or get_settings()
    rows = repository.list_for_indexing(repo, limit)
    counters = {"scanned": len(rows), "head_embedded": 0, "chunked": 0, "unchanged": 0}
    batch_size = settings.embedding_batch_size
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset : offset + batch_size]
        head_rows = [
            row
            for row in batch
            if not (
                str(row.get("embedding_content_hash") or "").strip()
                == str(row["content_hash"]).strip()
                and row.get("embedding_model") == provider.model_name
                and row.get("embedding_dimensions") == provider.dimensions
                and row.get("embedding_text_version") == ISSUE_EMBEDDING_TEXT_VERSION
            )
        ]
        if head_rows:
            texts = [issue_embedding_text(row["title"], row["body"]) for row in head_rows]
            vectors = provider.embed(texts)
            observations = list(provider.last_observations)
            for index, (row, vector) in enumerate(zip(head_rows, vectors, strict=True)):
                saved = repository.save_embedding(
                    row["id"], str(row["content_hash"]).strip(), provider.model_name,
                    provider.dimensions, vector, text_version=ISSUE_EMBEDDING_TEXT_VERSION,
                    observation=observations[index] if index < len(observations) else None,
                )
                counters["head_embedded"] += int(saved)

        prepared = []
        flat_texts = []
        for row in batch:
            chunk_matches = (
                row.get("chunk_strategy_version") == settings.embedding_chunk_strategy_version
                and row.get("chunk_embedding_model") == provider.model_name
                and row.get("tokenizer_name") == provider.model_name
                and row.get("chunk_size") == settings.embedding_chunk_size
                and row.get("chunk_overlap") == settings.embedding_chunk_overlap
                and (row.get("chunk_count") or 0) > 0
            )
            if chunk_matches:
                continue
            chunked = chunk_issue_text(
                row["title"], row["body"], provider,
                chunk_size=settings.embedding_chunk_size,
                overlap=settings.embedding_chunk_overlap,
                max_chunks=settings.embedding_max_chunks,
            )
            start = len(flat_texts)
            flat_texts.extend(chunk.text for chunk in chunked.chunks)
            prepared.append((row, chunked, start, len(flat_texts)))
        chunk_vectors = provider.embed(flat_texts) if flat_texts else []
        for row, chunked, start, end in prepared:
            saved = repository.save_chunks(
                row["id"], expected_content_hash=str(row["content_hash"]).strip(),
                chunks=chunked.chunks, vectors=chunk_vectors[start:end],
                strategy_version=settings.embedding_chunk_strategy_version,
                model_name=provider.model_name, tokenizer_name=provider.model_name,
                chunk_size=settings.embedding_chunk_size,
                chunk_overlap=settings.embedding_chunk_overlap,
                original_token_count=chunked.original_token_count,
                stored_token_count=chunked.stored_token_count,
                truncated_token_count=chunked.truncated_token_count,
            )
            counters["chunked"] += int(saved)
        counters["unchanged"] += sum(
            row not in head_rows and all(item[0] is not row for item in prepared)
            for row in batch
        )
    return {"repo": repo, **counters}
