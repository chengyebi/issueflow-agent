from app.rag.chunking import chunk_issue_text
from app.rag.embedding import FakeEmbeddingProvider


def test_chunking_repeats_title_and_reports_truncation():
    provider = FakeEmbeddingProvider(dimensions=8)
    body = " ".join(f"token{index}" for index in range(300))
    result = chunk_issue_text(
        "Crash on launch", body, provider, chunk_size=40, overlap=8, max_chunks=3
    )
    assert len(result.chunks) == 3
    assert all(chunk.text.startswith("Title: Crash on launch") for chunk in result.chunks)
    assert all(chunk.token_count <= 40 for chunk in result.chunks)
    assert result.truncated_token_count > 0
    assert result.original_token_count == result.stored_token_count + result.truncated_token_count


def test_empty_body_still_generates_title_chunk():
    result = chunk_issue_text(
        "Empty report", "", FakeEmbeddingProvider(dimensions=8), chunk_size=40, overlap=8
    )
    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_type == "title"
    assert "[empty]" in result.chunks[0].text


def test_unchanged_chunk_text_has_stable_hash():
    provider = FakeEmbeddingProvider(dimensions=8)
    first = chunk_issue_text("Title", "one two three", provider, chunk_size=40, overlap=8)
    second = chunk_issue_text(" Title ", "one  two three", provider, chunk_size=40, overlap=8)
    assert [item.content_hash for item in first.chunks] == [
        item.content_hash for item in second.chunks
    ]
