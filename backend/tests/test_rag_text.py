from app.rag.text import EMPTY_BODY_MARKER, issue_content_hash, issue_embedding_text


def test_issue_embedding_text_is_deterministic_and_excludes_labels():
    first = issue_embedding_text(
        "  Login\r\nerror  ",
        None,
        ["bug", " auth ", "bug"],
    )
    second = issue_embedding_text("Login\nerror", "", ["auth", "bug"])

    assert first == second
    assert f"Body:\n{EMPTY_BODY_MARKER}" in first
    assert "Labels:" not in first


def test_content_hash_ignores_labels_but_changes_with_retrieval_content():
    baseline = issue_content_hash("Title", "Body", ["bug"])
    assert baseline == issue_content_hash(" Title ", "Body\r\n", [" bug "])
    assert baseline == issue_content_hash("Title", "Body", ["enhancement"])
    assert baseline != issue_content_hash("Title", "Different body", ["bug"])
