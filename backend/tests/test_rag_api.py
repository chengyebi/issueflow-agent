def test_search_rejects_unknown_retrieval_mode_before_database_access(client):
    response = client.get(
        "/historical-issues/search",
        params={"repo": "owner/repo", "query": "login", "mode": "unknown"},
    )

    assert response.status_code == 422
