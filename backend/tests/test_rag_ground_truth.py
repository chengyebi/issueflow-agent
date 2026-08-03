from app.rag import ground_truth
from app.rag.ground_truth import (
    _time_aligned_corpus,
    collect_repository_ground_truth,
    extract_active_relations,
)


def _issue():
    return {"number": 20, "title": "Crash", "body": "details"}


def test_marked_duplicate_is_ground_truth_and_unmark_cancels_relation():
    timeline = [
        {
            "event": "marked_as_duplicate",
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "actor": {"login": "maintainer"},
            "source": {"issue": {"number": 10}},
        },
        {
            "event": "unmarked_as_duplicate",
            "id": 2,
            "created_at": "2026-01-02T00:00:00Z",
            "source": {"issue": {"number": 10}},
        },
    ]
    assert extract_active_relations("owner/repo", _issue(), timeline) == []


def test_only_maintainer_comment_is_accepted():
    timeline = [
        {
            "event": "commented",
            "id": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "author_association": "NONE",
            "body": "Duplicate of #10",
            "actor": {"login": "reporter"},
        },
        {
            "event": "commented",
            "id": 2,
            "created_at": "2026-01-02T00:00:00Z",
            "author_association": "MEMBER",
            "body": "/duplicate of #11",
            "actor": {"login": "maintainer"},
        },
    ]
    relations = extract_active_relations("owner/repo", _issue(), timeline)
    assert [item["target_issue_number"] for item in relations] == [11]
    assert relations[0]["operator_association"] == "MEMBER"


def test_collection_excludes_future_target_and_query_body_leakage(monkeypatch):
    summaries = [
        {
            "number": number,
            "title": f"Issue {number}",
            "body": "mentions #10" if number == 30 else "clean report",
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-03T00:00:00Z",
            "html_url": f"https://github.test/issues/{number}",
        }
        for number in (20, 30, 40)
    ]
    monkeypatch.setattr(ground_truth, "_candidate_issues", lambda repo, limit: summaries)

    def relations(repo, summary):
        target = 10 if summary["number"] != 40 else 50
        return [{
            "repo": repo,
            "query_issue_number": summary["number"],
            "target_issue_number": target,
            "evidence_source": "commented",
            "evidence_event_id": "1",
            "evidence_time": "2026-01-02T01:00:00Z",
            "operator_login": "maintainer",
            "operator_association": "MEMBER",
            "evidence_excerpt": f"Duplicate of #{target}",
        }]

    monkeypatch.setattr(ground_truth, "_candidate_relations", relations)

    def issue(repo, number):
        return {
            "number": number,
            "title": "Target",
            "body": "",
            "created_at": (
                "2026-01-01T00:00:00Z" if number == 10 else "2026-01-03T00:00:00Z"
            ),
            "html_url": f"https://github.test/issues/{number}",
        }

    monkeypatch.setattr(ground_truth, "get_repository_issue", issue)
    edges, audit, _ = collect_repository_ground_truth(
        "owner/repo", query_limit=10, search_limit=10
    )
    assert [item["query_issue_number"] for item in edges] == [20]
    reasons = {item["query_issue_number"]: item["exclusion_reason"] for item in audit}
    assert reasons[30] == "query_title_or_body_contains_target"
    assert reasons[40] == "target_not_older_than_query"


def test_candidate_search_filters_pull_requests_and_uses_duplicate_labels(monkeypatch):
    monkeypatch.setattr(
        ground_truth,
        "list_repository_labels",
        lambda repo: [{"name": "bug"}, {"name": "S-duplicate"}],
    )
    queries = []

    def search(query, **kwargs):
        queries.append(query)
        return [
            {"number": 1, "updated_at": "2026-01-01T00:00:00Z"},
            {"number": 2, "pull_request": {}, "updated_at": "2026-01-02T00:00:00Z"},
        ]

    monkeypatch.setattr(ground_truth, "search_issues", search)
    result = ground_truth._candidate_issues("owner/repo", 20)
    assert [item["number"] for item in result] == [1]
    assert any('label:"S-duplicate"' in query for query in queries)
    assert any("duplicate in:comments" in query for query in queries)


def test_time_aligned_corpus_windows_before_earliest_query(monkeypatch):
    calls = []

    def search(query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            return [
                {"number": 3, "created_at": "2025-12-20T00:00:00Z"},
                {"number": 2, "created_at": "2025-12-10T00:00:00Z"},
            ]
        return [{"number": 1, "created_at": "2025-11-01T00:00:00Z"}]

    monkeypatch.setattr(ground_truth, "search_issues", search)
    result = _time_aligned_corpus(
        "owner/repo", ground_truth._iso("2026-01-01T00:00:00Z"), 3
    )
    assert [item["number"] for item in result] == [3, 2, 1]
    assert "created:<2026-01-01" in calls[0]
    assert "created:<2025-12-10" in calls[1]
