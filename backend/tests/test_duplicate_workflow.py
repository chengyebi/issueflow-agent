from app.agents import workflow
from app.agents.workflow import (
    DuplicateJudgment,
    IssueAgentRequest,
    ReviewDraft,
    TriageResult,
)
from app.rag.schema import RetrievalResult, SimilarIssueCandidate


def _duplicate_state():
    return {
        "repo": "owner/repo",
        "issue_number": 99,
        "title": "Login crash",
        "body": "Wrong password returns 500",
        "category": "bug",
        "suggested_reply": "Please add logs",
        "duplicate_assessment": {
            "is_duplicate": True,
            "candidate_issue_number": 10,
            "confidence": 0.9,
            "rationale": "same failure",
        },
    }


def test_possible_duplicate_only_creates_review_recommendation(monkeypatch):
    state = _duplicate_state()
    state["similar_issues"] = [
        {"issue_number": 10, "title": "Login 500", "evidence": "same steps"}
    ]
    monkeypatch.setattr(
        workflow,
        "invoke_structured",
        lambda *args: DuplicateJudgment(
            is_duplicate=True,
            candidate_issue_number=10,
            confidence=0.9,
            rationale="same failure",
            evidence=["same trigger"],
        ),
    )
    judged = workflow.judge_duplicate(state)
    state.update(judged)
    actions = workflow.prepare_actions(state)
    assert actions["proposed_actions"] == []
    assert judged["review_recommendations"][0]["type"] == "review_possible_duplicate"
    assert "close" not in str(judged).lower()


def test_no_candidates_skips_duplicate_model_call(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "invoke_structured",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call model")),
    )
    result = workflow.judge_duplicate({"similar_issues": []})
    assert result["duplicate_assessment"]["is_duplicate"] is False


def test_high_risk_still_has_no_public_actions():
    result = workflow.security_review(_duplicate_state())
    assert result["proposed_actions"] == []


def test_langgraph_duplicate_path_reaches_human_review_without_public_commands(
    monkeypatch,
):
    class Retriever:
        def search(self, *args, **kwargs):
            return RetrievalResult(
                mode="hybrid",
                candidates=[
                    SimilarIssueCandidate(
                        historical_issue_id=1,
                        repo="owner/repo",
                        issue_number=10,
                        title="Existing login 500",
                        state="closed",
                        lexical_score=0.9,
                        vector_score=0.8,
                        rrf_score=0.03,
                        lexical_rank=1,
                        vector_rank=1,
                        sources=["lexical", "vector"],
                        evidence="wrong password crashes",
                    )
                ],
            )

    def structured(schema, messages):
        if schema is DuplicateJudgment:
            return DuplicateJudgment(
                is_duplicate=True,
                candidate_issue_number=10,
                confidence=0.95,
                rationale="same trigger and failure",
                evidence=["wrong password"],
            )
        if schema is TriageResult:
            return TriageResult(
                category="bug", priority="medium", risk_level="low", confidence=0.9
            )
        if schema is ReviewDraft:
            return ReviewDraft(
                missing_repro_fields=[], summary="summary", suggested_reply="reply"
            )
        raise AssertionError(schema)

    monkeypatch.setattr(workflow, "get_duplicate_retriever", lambda: Retriever())
    monkeypatch.setattr(workflow, "invoke_structured", structured)
    result = workflow.run_issue_agent(
        IssueAgentRequest(
            repo="owner/repo",
            issue_number=99,
            title="Login returns 500",
            body="wrong password crashes",
        )
    )
    assert result.duplicate_assessment.is_duplicate is True
    assert result.review_recommendations[0].candidate_issue_number == 10
    assert result.proposed_actions == []
