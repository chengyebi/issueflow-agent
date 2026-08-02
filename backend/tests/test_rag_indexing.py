from app.rag.embedding import FakeEmbeddingProvider
from app.rag.indexing import embed_historical_issue


class Repository:
    def __init__(self, embedded=False):
        self.issue = {
            "id": 1,
            "title": "Login 500",
            "body": "wrong password",
            "content_hash": "a" * 64,
            "embedding_content_hash": "a" * 64 if embedded else None,
            "embedding_model": "fake-hash-v1" if embedded else None,
            "embedding_dimensions": 8 if embedded else None,
        }
        self.saved = []

    def get_for_embedding(self, issue_id):
        return self.issue

    def save_embedding(self, *args):
        self.saved.append(args)
        return True


def test_indexing_skips_provider_when_content_and_model_are_unchanged():
    repository = Repository(embedded=True)
    provider = FakeEmbeddingProvider(dimensions=8)
    result = embed_historical_issue(1, repository=repository, provider=provider)
    assert result["status"] == "unchanged"
    assert provider.call_count == 0
    assert repository.saved == []


def test_indexing_generates_and_saves_embedding_for_changed_content():
    repository = Repository(embedded=False)
    provider = FakeEmbeddingProvider(dimensions=8)
    result = embed_historical_issue(1, repository=repository, provider=provider)
    assert result["status"] == "embedded"
    assert provider.call_count == 1
    assert len(repository.saved[0][-1]) == 8
