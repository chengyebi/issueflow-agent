from collections import defaultdict
from statistics import mean

from app.eval.schema import EvalCase, EvalPrediction


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _macro_f1(true_values: list[str], predicted_values: list[str | None]) -> float:
    labels = sorted(set(true_values) | {value for value in predicted_values if value})
    f1_values = []
    for label in labels:
        true_positive = sum(
            true == label and predicted == label
            for true, predicted in zip(true_values, predicted_values, strict=True)
        )
        false_positive = sum(
            true != label and predicted == label
            for true, predicted in zip(true_values, predicted_values, strict=True)
        )
        false_negative = sum(
            true == label and predicted != label
            for true, predicted in zip(true_values, predicted_values, strict=True)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1_values.append(
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
    return mean(f1_values) if f1_values else 0.0


def calculate_metrics(
    cases: list[EvalCase], predictions: list[EvalPrediction]
) -> dict:
    prediction_by_id = {prediction.case_id: prediction for prediction in predictions}
    aligned = [(case, prediction_by_id.get(case.id)) for case in cases]
    if any(prediction is None for _, prediction in aligned):
        missing = [case.id for case, prediction in aligned if prediction is None]
        raise ValueError(f"缺少评测预测: {', '.join(missing)}")

    valid_pairs = [(case, prediction) for case, prediction in aligned if prediction]
    categories = [case.expected.category for case, _ in valid_pairs]
    predicted_categories = [prediction.category for _, prediction in valid_pairs]
    correct = sum(
        prediction.category == case.expected.category for case, prediction in valid_pairs
    )
    high_risk_pairs = [
        (case, prediction)
        for case, prediction in valid_pairs
        if case.expected.risk_level == "high"
    ]
    costs = [
        prediction.estimated_cost_usd
        for _, prediction in valid_pairs
        if prediction.estimated_cost_usd is not None
    ]
    latency_values = [prediction.latency_ms for _, prediction in valid_pairs]
    token_values = [
        prediction.input_tokens + prediction.output_tokens
        for _, prediction in valid_pairs
    ]
    by_error_type: dict[str, int] = defaultdict(int)
    for _, prediction in valid_pairs:
        if prediction.error_type:
            by_error_type[prediction.error_type] += 1

    count = len(valid_pairs)
    return {
        "case_count": count,
        "accuracy": correct / count if count else None,
        "macro_f1": _macro_f1(categories, predicted_categories) if count else None,
        "high_risk_recall": (
            sum(prediction.risk_level == "high" for _, prediction in high_risk_pairs)
            / len(high_risk_pairs)
            if high_risk_pairs
            else None
        ),
        "structured_output_success_rate": (
            sum(prediction.structured_output_success for _, prediction in valid_pairs)
            / count
            if count
            else None
        ),
        "agent_success_rate": (
            sum(prediction.agent_success for _, prediction in valid_pairs) / count
            if count
            else None
        ),
        "latency_p50_ms": _percentile(latency_values, 0.5),
        "latency_p95_ms": _percentile(latency_values, 0.95),
        "average_tokens": mean(token_values) if token_values else None,
        "average_estimated_cost_usd": mean(costs) if costs else None,
        "error_type_counts": dict(sorted(by_error_type.items())),
    }
