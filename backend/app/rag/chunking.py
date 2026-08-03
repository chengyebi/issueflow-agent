import hashlib
from dataclasses import dataclass

from app.rag.embedding import EmbeddingProvider
from app.rag.text import EMPTY_BODY_MARKER, normalize_text


@dataclass(frozen=True)
class TextChunk:
    index: int
    chunk_type: str
    text: str
    token_count: int
    content_hash: str


@dataclass(frozen=True)
class ChunkedText:
    chunks: list[TextChunk]
    original_token_count: int
    stored_token_count: int
    truncated_token_count: int


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_issue_text(
    title: str,
    body: str | None,
    provider: EmbeddingProvider,
    *,
    chunk_size: int = 384,
    overlap: int = 64,
    max_chunks: int = 16,
) -> ChunkedText:
    """Split title/body with the embedding model tokenizer and observable truncation.

    Every chunk includes the normalized title. `stored_token_count` counts unique
    source tokens retained (not repeated title/overlap tokens).
    """
    if chunk_size < 32:
        raise ValueError("chunk_size 必须至少为 32 tokens")
    if not 0 <= overlap < chunk_size:
        raise ValueError("chunk_overlap 必须大于等于 0 且小于 chunk_size")
    if not 1 <= max_chunks <= 64:
        raise ValueError("max_chunks 必须在 1 到 64 之间")

    normalized_title = normalize_text(title)
    normalized_body = normalize_text(body)
    prefix = f"Title: {normalized_title}\nBody:\n"
    prefix_ids = provider.tokenize(prefix)
    source_body = normalized_body or EMPTY_BODY_MARKER
    body_ids = provider.tokenize(source_body)
    original_ids = provider.tokenize(f"{prefix}{source_body}")
    body_budget = chunk_size - len(prefix_ids)
    if body_budget <= 0:
        raise ValueError(
            f"Issue 标题占用 {len(prefix_ids)} tokens，已达到 chunk_size={chunk_size}"
        )
    effective_overlap = min(overlap, max(body_budget - 1, 0))
    chunks: list[TextChunk] = []
    retained_end = 0
    start = 0
    index = 0
    while start < len(body_ids) and index < max_chunks:
        window = body_ids[start : start + body_budget]
        decoded = provider.decode_tokens(window).strip() or EMPTY_BODY_MARKER
        text = f"{prefix}{decoded}"
        token_count = len(provider.tokenize(text))
        # Decode/re-encode is not always token-count preserving (notably WordPiece
        # punctuation/whitespace). Shrink explicitly instead of silently relying on
        # the model's 512-token truncation.
        while token_count > chunk_size and len(window) > 1:
            window = window[: -(max(1, token_count - chunk_size))]
            decoded = provider.decode_tokens(window).strip() or EMPTY_BODY_MARKER
            text = f"{prefix}{decoded}"
            token_count = len(provider.tokenize(text))
        if token_count > chunk_size:
            raise ValueError("标题与最小正文 Chunk 仍超过 chunk_size")
        chunks.append(
            TextChunk(
                index=index,
                chunk_type="title" if not normalized_body else "title_body",
                text=text,
                token_count=token_count,
                content_hash=_hash(text),
            )
        )
        retained_end = max(retained_end, min(start + len(window), len(body_ids)))
        if retained_end >= len(body_ids):
            break
        start += max(1, len(window) - min(effective_overlap, len(window) - 1))
        index += 1

    stored_source_tokens = min(len(prefix_ids) + retained_end, len(original_ids))
    return ChunkedText(
        chunks=chunks,
        original_token_count=len(original_ids),
        stored_token_count=stored_source_tokens,
        truncated_token_count=max(0, len(original_ids) - stored_source_tokens),
    )


def chunk_query_text(
    title: str,
    body: str | None,
    provider: EmbeddingProvider,
    *,
    chunk_size: int = 384,
    overlap: int = 64,
    max_chunks: int = 16,
) -> ChunkedText:
    return chunk_issue_text(
        title,
        body,
        provider,
        chunk_size=chunk_size,
        overlap=overlap,
        max_chunks=max_chunks,
    )
