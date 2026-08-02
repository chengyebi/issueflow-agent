from app.agents.workflow import security_review


def test_high_risk_branch_produces_no_public_commands():
    result = security_review({})
    assert result["status"] == "NEEDS_SECURITY_REVIEW"
    assert result["proposed_actions"] == []

