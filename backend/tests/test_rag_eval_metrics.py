from app.rag.eval_metrics import calculate_duplicate_metrics
from app.rag.eval_schema import DuplicateEvalCase, DuplicateEvalPrediction


def _case(case_id, relevant, duplicate):
    return DuplicateEvalCase.model_validate(
        {
            "id": case_id,
            "query": {
                "repo": "o/r",
                "issue_number": 100,
                "title": "query",
                "body": "body",
            },
            "expected": {
                "relevant_issue_numbers": relevant,
                "is_duplicate": duplicate,
            },
            "source": "test",
        }
    )


def test_duplicate_metrics_include_retrieval_judgment_and_latency():
    cases = [_case("a", [10], True), _case("b", [], False)]
    predictions = [
        DuplicateEvalPrediction(
            case_id="a",
            mode="hybrid",
            ranked_issue_numbers=[20, 10],
            predicted_is_duplicate=True,
            predicted_candidate_issue_number=20,
            latency_ms=10,
        ),
        DuplicateEvalPrediction(
            case_id="b",
            mode="hybrid",
            ranked_issue_numbers=[30],
            predicted_is_duplicate=False,
            latency_ms=30,
        ),
    ]
    metrics = calculate_duplicate_metrics(cases, predictions)
    assert metrics["recall_at_1"] == 0
    assert metrics["recall_at_5"] == 1
    assert metrics["mrr_at_10"] == 0.5
    assert metrics["precision_at_1"] == 0
    assert metrics["duplicate_precision"] == 1
    assert metrics["duplicate_recall"] == 1
    assert metrics["duplicate_f1"] == 1
    assert metrics["retrieval_p50_ms"] == 20
    assert metrics["retrieval_p95_ms"] == 29
