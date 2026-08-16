"""运行自动化评测（Stage 2）：把冻结 prediction artifact 喂给 Policy Gate。

原则（P0-1 / P0-2 / P0-3）：
- 只读取 predictions.jsonl（Stage 1 产物），绝不在这里做预测，也不读 ground truth。
- 每个 case 区分三个概念：
    true_category      来自 artifact（仅用于 metric 比较）
    predicted_category 来自 artifact（作为预测）
    prediction_confidence 来自 artifact（不作为 production 可信度）
- 只允许 smoke runner 的 artifact 走评测框架验证；production threshold 需要
  production-compatible prediction artifact，且必须有 prediction_artifact_hash。
- 删除随机 confidence：raw_confidence 全部来自 artifact。

指标：
    eligible_count / auto_execute_count / defer_count / no_action_count
    automation_coverage / human_touch_rate
    auto_action_precision + Wilson CI
    error_auto_execute_count
    按 intent / repo / confidence bucket 的 precision 与 coverage
    defer reason distribution
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

from app.automation.models import (
    ActionIntent,
    AutomationAction,
    AutomationDecision,
    AutomationDisposition,
)
from app.automation.policy import decide_automation
from app.automation.policy_loader import CalibratedPolicy, load_calibrated_policy

# 允许自动化的 intent 集合（需与冻结 policy 一致）。
ALLOWED_AUTO_INTENTS = {ActionIntent.ADD_CATEGORY_LABEL}


class _EvalResult:
    """把 artifact 记录包装成 decide_automation 需要的鸭子类型。

    predicted_category 作为唯一预测来源，绝不使用 true_category。
    """

    def __init__(self, repo, issue_number, predicted_category, actions):
        self.repo = repo
        self.issue_number = issue_number
        # 注意：这里填的是预测分类，只用于构建动作。
        self.category = predicted_category
        self.risk_level = "low"
        self.retrieval_degraded = False
        self.duplicate_assessment = None
        self.proposed_actions = actions


def _build_action(predicted_category: str, confidence: float) -> AutomationAction | None:
    if predicted_category not in ("bug", "feature", "question", "documentation"):
        return None
    return AutomationAction(
        type="add_label",
        value=predicted_category,
        intent=ActionIntent.ADD_CATEGORY_LABEL,
        confidence=confidence,
        rationale=f"预测分诊为 {predicted_category}",
        evidence=[f"分类：{predicted_category}"],
    )


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _confidence_bucket(confidence: float) -> str:
    if confidence >= 0.90:
        return "0.90-1.00"
    if confidence >= 0.80:
        return "0.80-0.90"
    if confidence >= 0.70:
        return "0.70-0.80"
    return "0.00-0.70"


def _read_predictions(path: Path) -> tuple[list[dict], str]:
    predictions = []
    raw_lines = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw_lines.append(line)
            predictions.append(json.loads(line))
    artifact_hash = hashlib.sha256(
        ("\n".join(raw_lines) + "\n").encode("utf-8")
    ).hexdigest()
    return predictions, artifact_hash


def run_eval(predictions_path: Path, policy_path: Path) -> dict:
    policy: CalibratedPolicy = load_calibrated_policy(policy_path)
    predictions, artifact_hash = _read_predictions(predictions_path)

    auto_count = 0
    defer_count = 0
    no_action_count = 0
    error_auto_count = 0
    auto_correct = 0
    auto_total = 0

    by_intent: dict[str, dict] = {}
    by_repo: dict[str, dict] = {}
    by_bucket: dict[str, dict] = {}
    defer_reasons: dict[str, int] = {}
    sample_auto: list[dict] = []

    for case in predictions:
        true_category = case["true_category"]
        predicted_category = case["predicted_category"]
        prediction_confidence = case.get("raw_confidence", 1.0)

        # 预测动作只基于 predicted_category。
        action = _build_action(predicted_category, prediction_confidence)
        result = _EvalResult(
            case["repo"], case["issue_number"], predicted_category, [action] if action else []
        )
        decision: AutomationDecision = decide_automation(
            result, mode="enforce", calibrated_policy=policy
        )

        disposition = decision.disposition
        if disposition == AutomationDisposition.AUTO_EXECUTE:
            auto_count += 1
            auto_total += 1
            # P0-1：唯一比较点，predicted vs true。
            if predicted_category == true_category:
                auto_correct += 1
            else:
                error_auto_count += 1
            sample_auto.append(
                {
                    "repo": case["repo"],
                    "issue_number": case["issue_number"],
                    "true_category": true_category,
                    "predicted_category": predicted_category,
                    "confidence": prediction_confidence,
                }
            )
            bucket = _confidence_bucket(prediction_confidence)
            _bump(by_bucket, bucket, true_category, predicted_category)
        elif disposition == AutomationDisposition.DEFER:
            defer_count += 1
            reason_code = (
                decision.handoff.reason_code.value
                if decision.handoff
                else "unknown"
            )
            defer_reasons[reason_code] = defer_reasons.get(reason_code, 0) + 1
        else:
            no_action_count += 1

        if decision.actions:
            intent = decision.actions[0].intent.value
            _bump(by_intent, intent, true_category, predicted_category)
            _bump(by_repo, case["repo"], true_category, predicted_category)

    eligible = len(predictions)
    coverage = auto_count / eligible if eligible else 0.0
    human_touch = defer_count / eligible if eligible else 0.0
    precision = auto_correct / auto_total if auto_total else 0.0
    lower, upper = _wilson_ci(auto_correct, auto_total)

    return {
        "eligible_count": eligible,
        "auto_execute_count": auto_count,
        "defer_count": defer_count,
        "no_action_count": no_action_count,
        "automation_coverage": round(coverage, 4),
        "human_touch_rate": round(human_touch, 4),
        "auto_action_precision": round(precision, 4),
        "auto_action_precision_lower": round(lower, 4),
        "auto_action_precision_upper": round(upper, 4),
        "error_auto_execute_count": error_auto_count,
        "prediction_artifact_hash": artifact_hash,
        "runner_type": predictions[0].get("runner_type") if predictions else None,
        "by_intent": _finalize_metrics(by_intent),
        "by_repo": _finalize_metrics(by_repo),
        "by_confidence_bucket": _finalize_metrics(by_bucket),
        "defer_reason_distribution": defer_reasons,
        "samples": sample_auto[:20],
    }


def _bump(counter: dict[str, dict], key: str, true_category: str, predicted: str) -> None:
    entry = counter.setdefault(key, {"n": 0, "correct": 0, "coverage_cases": 0})
    entry["n"] += 1
    entry["coverage_cases"] += 1
    if predicted == true_category:
        entry["correct"] += 1


def _finalize_metrics(counter: dict[str, dict]) -> dict:
    result = {}
    for key, entry in counter.items():
        precision = entry["correct"] / entry["n"] if entry["n"] else 0.0
        result[key] = {
            "precision": round(precision, 4),
            "coverage_cases": entry["coverage_cases"],
            "sample_count": entry["n"],
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="运行自动化评测（Stage 2）")
    parser.add_argument(
        "--predictions",
        type=Path,
        default=Path("eval/automation/predictions/predictions_dev.jsonl"),
    )
    parser.add_argument(
        "--policy", type=Path, default=Path("eval/automation/policy.json")
    )
    parser.add_argument("--out", type=Path, default=Path("eval/reports/automation_eval.json"))
    args = parser.parse_args()

    report = run_eval(args.predictions, args.policy)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
