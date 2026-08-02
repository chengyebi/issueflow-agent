from statistics import mean

from app.rag.eval_schema import DuplicateEvalCase, DuplicateEvalPrediction


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def calculate_duplicate_metrics(
    cases: list[DuplicateEvalCase], predictions: list[DuplicateEvalPrediction]
) -> dict:
    case_by_id = {case.id: case for case in cases}
    relevant_cases = [case for case in cases if case.expected.relevant_issue_numbers]
    recalls_at_1 = []
    recalls_at_5 = []
    reciprocal_ranks = []
    precision_at_1 = []
    true_positive = false_positive = false_negative = 0
    for prediction in predictions:
        case = case_by_id[prediction.case_id]
        relevant = set(case.expected.relevant_issue_numbers)
        if relevant:
            ranked = prediction.ranked_issue_numbers
            recalls_at_1.append(len(relevant & set(ranked[:1])) / len(relevant))
            recalls_at_5.append(len(relevant & set(ranked[:5])) / len(relevant))
            first_rank = next(
                (rank for rank, number in enumerate(ranked[:10], 1) if number in relevant),
                None,
            )
            reciprocal_ranks.append(1 / first_rank if first_rank else 0.0)
            precision_at_1.append(1.0 if ranked[:1] and ranked[0] in relevant else 0.0)
        if prediction.predicted_is_duplicate and case.expected.is_duplicate:
            true_positive += 1
        elif prediction.predicted_is_duplicate and not case.expected.is_duplicate:
            false_positive += 1
        elif not prediction.predicted_is_duplicate and case.expected.is_duplicate:
            false_negative += 1

    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else None
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else None
    )
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    latencies = [prediction.latency_ms for prediction in predictions]
    return {
        "case_count": len(cases),
        "relevant_case_count": len(relevant_cases),
        "recall_at_1": mean(recalls_at_1) if recalls_at_1 else None,
        "recall_at_5": mean(recalls_at_5) if recalls_at_5 else None,
        "mrr_at_10": mean(reciprocal_ranks) if reciprocal_ranks else None,
        "precision_at_1": mean(precision_at_1) if precision_at_1 else None,
        "duplicate_precision": precision,
        "duplicate_recall": recall,
        "duplicate_f1": f1,
        "retrieval_p50_ms": _percentile(latencies, 0.5),
        "retrieval_p95_ms": _percentile(latencies, 0.95),
        "degraded_count": sum(prediction.degraded for prediction in predictions),
    }
