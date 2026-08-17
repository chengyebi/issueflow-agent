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
    def __init__(self, repo, issue_number, predicted_category, actions, risk_level="low"):
        self.repo = repo
        self.issue_number = issue_number
        self.category = predicted_category
        # 2.3：使用真实 predicted_risk_level，不能硬编码 low。
        self.risk_level = risk_level
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
    high_risk_defer = 0
    unsupported_defer = 0
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
        predicted_risk_level = case.get("predicted_risk_level", "low")

        # 2.3：structured output failure 永远不能 auto。
        action = (
            _build_action(case["repo"], predicted_category, raw_confidence)
            if structured_success
            else None
        )
        result = _EvalResult(
            case["repo"], case["issue_number"], predicted_category,
            [action] if action else [],
            risk_level=predicted_risk_level,
        )
        decision = decide_automation(result, mode="enforce", calibrated_policy=policy)
        if decision.disposition != AutomationDisposition.AUTO_EXECUTE:
            # 统计为什么 DEFER：high-risk / unsupported。
            if predicted_risk_level == "high":
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
        "threshold": threshold,
        "eligible": len(predictions),
        "auto_count": auto_total,
        "coverage": round(auto_total / len(predictions), 4) if predictions else 0.0,
        "precision": round(precision, 4),
        "ci_lower": round(lower, 4),
        "ci_upper": round(upper, 4),
        "error_count": error_auto,
        "high_risk_defer_count": high_risk_defer,
        "unsupported_defer_count": unsupported_defer,
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


def _conf_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    s = sorted(values)
    n = len(s)

    def pct(p):
        return round(s[min(n - 1, int(p * n))], 4)

    return {
        "count": n,
        "mean": round(sum(s) / n, 4),
        "median": pct(0.50),
        "p10": pct(0.10),
        "p25": pct(0.25),
        "p75": pct(0.75),
        "p90": pct(0.90),
    }


def confidence_analysis(predictions: list[dict]) -> dict:
    """P2.4：raw confidence 是否有选择能力（correct vs incorrect 分布）。"""
    correct_confs = []
    incorrect_confs = []
    for case in predictions:
        conf = case.get("raw_confidence")
        if conf is None or case.get("predicted_category") is None:
            continue
        correct = (
            case.get("predicted_category") == case.get("true_category")
            and case.get("resolved_label") == case.get("expected_label")
        )
        (correct_confs if correct else incorrect_confs).append(conf)
    return {
        "correct": _conf_summary(correct_confs),
        "incorrect": _conf_summary(incorrect_confs),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="P2 calibration report")
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument(
        "--thresholds", type=str, default="all",
        help="逗号分隔阈值，或 'all' 使用全部 unique confidence breakpoint + 边界",
    )
    args = parser.parse_args()

    preds = _load_predictions(args.predictions)
    if args.thresholds.strip().lower() == "all":
        confs = sorted(
            {r["raw_confidence"] for r in preds if r.get("raw_confidence") is not None}
        )
        thresholds = sorted(set([0.0] + confs + [1.0]))
    else:
        thresholds = [float(t) for t in args.thresholds.split(",")]
    failures = [p for p in preds if not p.get("structured_output_success", True)]
    print(json.dumps({
        "prediction_count": len(preds),
        "structured_output_failure_count": len(failures),
        "confidence_analysis": confidence_analysis(preds),
        "threshold_curve": [_score(preds, t) for t in thresholds],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
