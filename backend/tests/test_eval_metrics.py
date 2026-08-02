import pytest

from app.eval.metrics import calculate_metrics
from app.eval.schema import EvalCase, EvalPrediction


def _case(case_id: str, category: str, risk: str = "low") -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": case_id,
            "input": {
                "repo": "o/r",
                "issue_number": 1,
                "title": "t",
                "body": "b",
            },
            "expected": {
                "category": category,
                "priority": "medium",
                "risk_level": risk,
            },
            "metadata": {"source": "test"},
        }
    )


def test_metrics_are_calculated_from_predictions():
    cases = [_case("1", "bug"), _case("2", "feature", "high")]
    predictions = [
        EvalPrediction(
            case_id="1",
            category="bug",
            priority="medium",
            risk_level="low",
            structured_output_success=True,
            agent_success=True,
            latency_ms=10,
            input_tokens=10,
            output_tokens=5,
        ),
        EvalPrediction(
            case_id="2",
            category="bug",
            priority="medium",
            risk_level="low",
            structured_output_success=False,
            agent_success=False,
            latency_ms=30,
            input_tokens=20,
            output_tokens=5,
            error_type="ValidationError",
        ),
    ]
    metrics = calculate_metrics(cases, predictions)
    assert metrics["accuracy"] == 0.5
    assert metrics["macro_f1"] == pytest.approx(1 / 3)
    assert metrics["high_risk_recall"] == 0.0
    assert metrics["structured_output_success_rate"] == 0.5
    assert metrics["agent_success_rate"] == 0.5
    assert metrics["latency_p50_ms"] == 20
    assert metrics["latency_p95_ms"] == 29
    assert metrics["average_tokens"] == 20
    assert metrics["average_estimated_cost_usd"] is None


def test_missing_prediction_is_rejected():
    with pytest.raises(ValueError, match="缺少评测预测"):
        calculate_metrics([_case("1", "bug")], [])
