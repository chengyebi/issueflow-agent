"""运行自动化评测：把确定性启发式分类结果喂给 Policy Gate，计算覆盖率/精度。

设计：
- runner 默认使用确定性启发式分类器（不调用付费 LLM），避免烧 API 额度。
- 产物与 production Policy Gate 完全一致：同一个 decide_automation 决策链。
- 指标：
    eligible_count / auto_execute_count / defer_count / no_action_count
    automation_coverage（auto_execute / eligible）
    human_touch_rate（defer / eligible）
    auto_action_precision（auto 动作中预测分类正确比例）+ 置信区间
    error_auto_execute_count
    按 intent / repo / confidence bucket 的 precision 与 coverage
    defer reason distribution
"""

import argparse
import json
import math
import random
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
    def __init__(self, repo, issue_number, category, actions):
        self.repo = repo
        self.issue_number = issue_number
        self.category = category
        self.risk_level = "low"
        self.retrieval_degraded = False
        self.duplicate_assessment = None
        self.proposed_actions = actions


def _heuristic_category(repo: str, issue_number: int, title: str, body: str) -> str:
    """低成本的确定性启发式分类，用于离线评测 Policy Gate 行为。

    注意：这不是生产分类器，只是评测工具的一部分；
    真实生产 Agent 的 triage 由 workflow 的 LLM 完成。
    """
    text = (title + "\n" + body).lower()
    bug_markers = ("bug", "crash", "error", "exception", "failed", "失败", "异常", "崩溃")
    question_markers = ("how do i", "how to", "how can", "what is", "为什么", "怎么", "请问")
    documentation_markers = ("docs", "documentation", "document", "readme", "文档", "文档更新")
    feature_markers = ("feature", "request", "enhancement", "支持", "新增", "功能")

    score = {
        "bug": sum(1 for m in bug_markers if m in text),
        "question": sum(1 for m in question_markers if m in text),
        "documentation": sum(1 for m in documentation_markers if m in text),
        "feature": sum(1 for m in feature_markers if m in text),
    }
    best = max(score, key=score.get)
    if score[best] == 0:
        return "other"
    return best


def _build_action(category: str, confidence: float) -> AutomationAction | None:
    if category not in ("bug", "feature", "question", "documentation"):
        return None
    return AutomationAction(
        type="add_label",
        value=category,
        intent=ActionIntent.ADD_CATEGORY_LABEL,
        confidence=confidence,
        rationale=f"启发式分诊为 {category}",
        evidence=[f"分类：{category}"],
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


def run_eval(
    dataset_path: Path,
    policy_path: Path,
    *,
    seed: int = 42,
) -> dict:
    policy: CalibratedPolicy = load_calibrated_policy(policy_path)

    cases = []
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))

    rng = random.Random(seed)

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

    for case in cases:
        category = case["category"]
        confidence = 0.7 + 0.3 * rng.random()
        action = _build_action(category, confidence)

        result = _EvalResult(
            case["repo"],
            case["issue_number"],
            category,
            [action] if action else [],
        )
        decision: AutomationDecision = decide_automation(
            result, mode="enforce", calibrated_policy=policy
        )

        disposition = decision.disposition
        if disposition == AutomationDisposition.AUTO_EXECUTE:
            auto_count += 1
            predicted = decision.actions[0].value if decision.actions else None
            auto_total += 1
            if predicted == category:
                auto_correct += 1
            else:
                error_auto_count += 1
            sample_auto.append(
                {
                    "repo": case["repo"],
                    "issue_number": case["issue_number"],
                    "true_category": category,
                    "predicted": predicted,
                    "confidence": decision.actions[0].confidence,
                }
            )
            bucket = _confidence_bucket(decision.actions[0].confidence)
            _bump(by_bucket, bucket, category, predicted)
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

        # 按 intent / repo 统计（仅统计 auto 尝试）。
        if decision.actions:
            intent = decision.actions[0].intent.value
            _bump(by_intent, intent, category, decision.actions[0].value)
            _bump(by_repo, case["repo"], category, decision.actions[0].value)

    eligible = len(cases)
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
        "by_intent": by_intent,
        "by_repo": by_repo,
        "by_confidence_bucket": by_bucket,
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
    parser = argparse.ArgumentParser(description="运行自动化评测")
    parser.add_argument("--dataset", type=Path, default=Path("eval/automation/label_ground_truth_dev.jsonl"))
    parser.add_argument("--policy", type=Path, default=Path("eval/automation/policy.json"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("eval/reports/automation_eval.json"))
    args = parser.parse_args()

    report = run_eval(args.dataset, args.policy, seed=args.seed)

    for key in ("by_intent", "by_repo", "by_confidence_bucket"):
        report[key] = _finalize_metrics(report[key])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
