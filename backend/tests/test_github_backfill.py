from app.services import github


def test_github_backfill_paginates_and_preserves_pull_request_marker(monkeypatch):
    pages = {
        1: [{"number": 1}, {"number": 2, "pull_request": {"url": "pr"}}],
        2: [{"number": 3}],
    }

    def request(method, path, payload=None):
        page = int(path.rsplit("page=", 1)[1])
        return pages[page]

    monkeypatch.setattr(github, "_request", request)
    issues = list(github.list_repository_issues("owner/repo", per_page=2))
    assert [item["number"] for item in issues] == [1, 2, 3]
    assert issues[1]["pull_request"] == {"url": "pr"}
