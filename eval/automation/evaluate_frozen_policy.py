"""TEST 一次性 evaluation：只评估一个 frozen policy。

输入：
- frozen policy artifact（policy.label.frozen.json，含 threshold + 完整 hash 链）
- TEST prediction artifact

行为：
- 只用 frozen policy 里锁死的 threshold 评估，禁止扫描/选择 threshold。
- 输出最终指标：LABEL_AUTO_ACTION_PRECISION / LABEL_AUTOMATION_COVERAGE / 95% CI /
  auto/correct/wrong/structured failures/high-risk defer/unsupported defer。
- per repo / per category / per repo+category 细分。
"""

import argparse
import json
from pathlib import Path

from app.automation.models import ActionIntent, AutomationAction, AutomationDisposition
from app.automation.policy import decide_automation
from app.automation.policy_loader import CalibratedPolicy, load_calibrated_policy
from app.automation.repo_labels import resolve_category_label
from calibration_report import _wilson_ci


def _load_predictions(path: Path) -> list[dict]:
    preds = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                preds.append(json.loads(line))
    return preds


class _EvalResult:
    def __init__(self, repo, issue_number, predicted_category, actions, risk_level):
        self.repo = repo
        self.issue_number = issue_number
        self.category = predicted_category
        self.risk_level = risk_level
        self.retrieval_degraded = False
        self.duplicate_assessment = None
        self.proposed_actions = actions


def evaluate_frozen(policy_path: Path, predictions_path: Path) -> dict:
    policy: CalibratedPolicy = load_calibrated_policy(policy_path)
    preds = _load_predictions(predictions_path)

    auto_correct = 0
    auto_total = 0
    error_auto = 0
    high_risk_defer = 0
    unsupported_defer = 0
    by_repo: dict[str, list] = {}
    by_category: dict[str, list] = {}
    by_repo_category: dict[str, list] = {}

    for case in preds:
        true_category = case["true_category"]
        expected_label = case["expected_label"]
        predicted_category = case["predicted_category"]
        resolved_label = case["resolved_label"]
        raw_confidence = case.get("raw_confidence")
        structured_success = case.get("structured_output_success", True)
        risk_level = case.get("predicted_risk_level", "low")

        action = None
        if structured_success and predicted_category is not None:
            label = resolve_category_label(case["repo"], predicted_category)
            if label is not None:
                action = AutomationAction(
                    type="add_label",
                    value=label,
                    intent=ActionIntent.ADD_CATEGORY_LABEL,
                    confidence=raw_confidence if raw_confidence is not None else 0.0,
                    rationale="frozen policy evaluation",
                    evidence=["production triage"],
                )
        result = _EvalResult(
            case["repo"], case["issue_number"], predicted_category,
            [action] if action else [], risk_level,
        )
        decision = decide_automation(result, mode="enforce", calibrated_policy=policy)
        if decision.disposition != AutomationDisposition.AUTO_EXECUTE:
            if risk_level == "high":
                high_risk_defer += 1
            elif decision.handoff is not None and decision.handoff.reason_code.value == "unsupported_action":
                unsupported_defer += 1
            continue
        auto_total += 1
        correct = (
            structured_success
            and predicted_category == true_category
            and resolved_label == expected_label
        )
        if correct:
            auto_correct += 1
        else:
            error_auto += 1
        by_repo.setdefault(case["repo"], []).append(correct)
        by_category.setdefault(true_category, []).append(correct)
        by_repo_category.setdefault(f"{case['repo']}/{true_category}", []).append(correct)

    precision = auto_correct / auto_total if auto_total else 0.0
    lower, upper = _wilson_ci(auto_correct, auto_total)
    return {
        "TEST_COUNT": len(preds),
        "LABEL_AUTO_ACTION_PRECISION": round(precision, 4),
        "LABEL_AUTO_ACTION_PRECISION_95CI": [round(lower, 4), round(upper, 4)],
        "LABEL_AUTOMATION_COVERAGE": round(auto_total / len(preds), 4) if preds else 0.0,
        "AUTO_COUNT": auto_total,
        "CORRECT_AUTO": auto_correct,
        "WRONG_AUTO": error_auto,
        "STRUCTURED_OUTPUT_FAILURES": sum(1 for p in preds if not p.get("structured_output_success", True)),
        "HIGH_RISK_DEFER_COUNT": high_risk_defer,
        "UNSUPPORTED_ACTION_DEFER_COUNT": unsupported_defer,
        "threshold": policy.rules["add_category_label"].min_model_confidence,
        "policy_version": policy.policy_version,
        "by_repo": _finalize(by_repo),
        "by_category": _finalize(by_category),
        "by_repo_category": _finalize(by_repo_category),
    }


def _finalize(buckets: dict[str, list]) -> dict:
    out = {}
    for key, flags in buckets.items():
        n = len(flags)
        k = sum(1 for f in flags if f)
        p = k / n if n else 0.0
        lo, hi = _wilson_ci(k, n)
        out[key] = {
            "sample_count": n,
            "auto_count": n,
            "precision": round(p, 4),
            "ci_lower": round(lo, 4),
            "ci_upper": round(hi, 4),
            "error_count": n - k,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="TEST 一次性 frozen policy evaluation")
    parser.add_argument("--policy", type=Path, required=True,
                        help="frozen policy artifact（含 threshold）")
    parser.add_argument("--predictions", type=Path, required=True,
                        help="TEST prediction artifact")
    args = parser.parse_args()
    print(json.dumps(evaluate_frozen(args.policy, args.predictions), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
