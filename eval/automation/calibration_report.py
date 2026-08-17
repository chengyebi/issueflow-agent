"""P2.4/P2.5：production prediction 的 calibration 报告。

只读一份 frozen prediction artifact（生产 LLM 生成），
输出：
  - overall precision / coverage / Wilson CI
  - per repo / per category / per repo-category 细分
  - confidence 是否真的有选择能力（correct vs incorrect 分布 + 阈值扫描）
  - structured output failure 统计
  - 推荐 threshold 或"无法可靠选择"

绝不对 TEST 运行。
"""

import argparse
import json
import math
from pathlib import Path

from app.automation.policy import decide_automation
from app.automation.models import ActionIntent, AutomationAction, AutomationDisposition
from app.automation.policy_loader import ALL_INTENTS, POLICY_SCHEMA_VERSION, CalibratedPolicy
from app.automation.repo_labels import resolve_category_label


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _load_predictions(path: Path) -> list[dict]:
    preds = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                preds.append(json.loads(line))
    return preds


class _EvalResult:
    def __init__(self, repo, issue_number, predicted_category, actions):
        self.repo = repo
        self.issue_number = issue_number
        self.category = predicted_category
        self.risk_level = "low"
        self.retrieval_degraded = False
        self.duplicate_assessment = None
        self.proposed_actions = actions


def _build_action(repo: str, predicted_category: str, confidence: float):
    if predicted_category is None:
        return None
    label = resolve_category_label(repo, predicted_category)
    if label is None:
        return None
    return AutomationAction(
        type="add_label",
        value=label,
        intent=ActionIntent.ADD_CATEGORY_LABEL,
        confidence=confidence if confidence is not None else 0.0,
        rationale="production prediction",
        evidence=["production triage"],
    )


def _temporary_policy(threshold: float) -> Path:
    import tempfile

    rules = {}
    for intent in ALL_INTENTS:
        rules[intent.value] = {
            "enabled": True,
            "min_model_confidence": threshold,
            "require_evidence": True,
            "observed_precision": None,
            "coverage": None,
            "sample_count": 0,
            "allow_auto": intent.value == "add_category_label",
        }
    data = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_version": f"calib-{threshold}",
        "created_at": "2026-08-17T00:00:00Z",
        "source_dataset_hash": "scan",
        "rules": rules,
    }
    tmp = Path(tempfile.gettempdir()) / f"calib-policy-{threshold}.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    return tmp


def _score(predictions: list[dict], threshold: float) -> dict:
    policy = CalibratedPolicy.model_validate(
        json.loads(_temporary_policy(threshold).read_text(encoding="utf-8"))
    )
    auto_correct = 0
    auto_total = 0
    error_auto = 0
    by_repo: dict[str, list] = {}
    by_category: dict[str, list] = {}
    by_repo_category: dict[str, list] = {}

    for case in predictions:
        true_category = case["true_category"]
        expected_label = case["expected_label"]
        predicted_category = case["predicted_category"]
        resolved_label = case["resolved_label"]
        raw_confidence = case.get("raw_confidence")
        structured_success = case.get("structured_output_success", True)

        action = _build_action(case["repo"], predicted_category, raw_confidence)
        result = _EvalResult(
            case["repo"], case["issue_number"], predicted_category, [action] if action else []
        )
        decision = decide_automation(result, mode="enforce", calibrated_policy=policy)
        if decision.disposition != AutomationDisposition.AUTO_EXECUTE:
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
        "threshold": threshold,
        "eligible": len(predictions),
        "auto_count": auto_total,
        "coverage": round(auto_total / len(predictions), 4) if predictions else 0.0,
        "precision": round(precision, 4),
        "ci_lower": round(lower, 4),
        "ci_upper": round(upper, 4),
        "error_count": error_auto,
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


def confidence_analysis(predictions: list[dict]) -> dict:
    """P2.4：raw confidence 是否有选择能力。"""
    correct_confs = []
    incorrect_confs = []
    for case in predictions:
        conf = case.get("raw_confidence")
        if conf is None:
            continue
        correct = (
            case.get("predicted_category") == case.get("true_category")
            and case.get("resolved_label") == case.get("expected_label")
        )
        if case.get("predicted_category") is None:
            continue
        (correct_confs if correct else incorrect_confs).append(conf)
    return {
        "correct_conf_mean": round(sum(correct_confs) / len(correct_confs), 4) if correct_confs else None,
        "correct_conf_count": len(correct_confs),
        "incorrect_conf_mean": round(sum(incorrect_confs) / len(incorrect_confs), 4) if incorrect_confs else None,
        "incorrect_conf_count": len(incorrect_confs),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="P2 calibration report")
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--thresholds", type=str, default="0.0,0.7,0.8,0.9,0.95,0.99")
    args = parser.parse_args()

    preds = _load_predictions(args.predictions)
    failures = [p for p in preds if not p.get("structured_output_success", True)]
    print(json.dumps({
        "prediction_count": len(preds),
        "structured_output_failure_count": len(failures),
        "confidence_analysis": confidence_analysis(preds),
        "threshold_curve": [
            _score(preds, float(t))
            for t in args.thresholds.split(",")
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
