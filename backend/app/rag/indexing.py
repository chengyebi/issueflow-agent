from app.rag.embedding import EmbeddingProvider, create_embedding_provider
from app.rag.repository import PostgresHistoricalIssueRepository


def issue_embedding_text(title: str, body: str) -> str:
    return f"标题：{title.strip()}\n正文：{body.strip()}"


def embed_historical_issue(
    historical_issue_id: int,
    *,
    repository: PostgresHistoricalIssueRepository | None = None,
    provider: EmbeddingProvider | None = None,
) -> dict:
    repository = repository or PostgresHistoricalIssueRepository()
    provider = provider or create_embedding_provider()
    issue = repository.get_for_embedding(historical_issue_id)
    if issue is None:
        raise ValueError(f"历史 Issue 不存在: {historical_issue_id}")
    if (
        issue["embedding_content_hash"] == issue["content_hash"]
        and issue["embedding_model"] == provider.model_name
        and issue["embedding_dimensions"] == provider.dimensions
    ):
        return {"historical_issue_id": historical_issue_id, "status": "unchanged"}
    vector = provider.embed([issue_embedding_text(issue["title"], issue["body"])])[0]
    saved = repository.save_embedding(
        historical_issue_id,
        str(issue["content_hash"]).strip(),
        provider.model_name,
        provider.dimensions,
        vector,
    )
    return {
        "historical_issue_id": historical_issue_id,
        "status": "embedded" if saved else "stale",
        "model": provider.model_name,
        "dimensions": provider.dimensions,
    }
