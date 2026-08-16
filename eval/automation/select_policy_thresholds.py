"""在 DEV 上选择 policy 阈值并冻结 artifact。

- 只读取 DEV 数据集，禁止读取 TEST（阈值冻结前不得查看）。
- 扫描置信度阈值，选择满足“错误自动执行数最小”且覆盖率最高的阈值。
- 输出冻结的 policy.json（含 observed_precision / coverage / sample_count）。
- 真实 calibration 需要人工标注或可信 ground truth；本工具在
  DEV ground truth 上估计精度，作为生产 artifact 的候选，需人工复核后启用。
"""

import argparse
import json
from pathlib import Path

from app.automation.policy_loader import ALL_INTENTS
from run_automation_eval import run_eval

THRESHOLD_CANDIDATES = [0.0, 0.70, 0.80, 0.90, 0.95]


def select_threshold(dataset_path: Path) -> dict:
    """返回每个候选阈值的 {coverage, error_count, precision}。"""
    results = {}
    for threshold in THRESHOLD_CANDIDATES:
        policy_file = _temporary_policy(threshold)
        try:
            report = run_eval(dataset_path, policy_file)
        finally:
            pass
        results[str(threshold)] = {
            "automation_coverage": report["automation_coverage"],
            "error_auto_execute_count": report["error_auto_execute_count"],
            "auto_action_precision": report["auto_action_precision"],
            "auto_execute_count": report["auto_execute_count"],
        }
    return results


def _temporary_policy(threshold: float) -> Path:
    """生成临时 policy：仅 add_category_label 允许自动执行。"""
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
            "allow_auto": True if intent.value == "add_category_label" else False,
        }
    data = {
        "schema_version": "1.0",
        "policy_version": f"threshold-{threshold}",
        "created_at": "2026-08-17T00:00:00Z",
        "source_dataset_hash": "",
        "rules": rules,
    }
    tmp = Path(tempfile.gettempdir()) / f"policy-t{threshold}.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    return tmp


def freeze_best(dataset_path: Path, out_path: Path) -> None:
    """在 DEV 上选最佳阈值并冻结 artifact。"""
    results = select_threshold(dataset_path)

    # 选择：错误自动执行数为 0 的前提下，覆盖率最高。
    feasible = [
        (t, r) for t, r in results.items() if r["error_auto_execute_count"] == 0
    ]
    if not feasible:
        # 全部阈值都有错误时，选错误最少、覆盖率最高。
        feasible = sorted(
            results.items(),
            key=lambda kv: (kv[1]["error_auto_execute_count"], -kv[1]["automation_coverage"]),
        )
        best = feasible[0]
    else:
        best = max(feasible, key=lambda kv: kv[1]["automation_coverage"])

    threshold = float(best[0])
    best_metrics = best[1]

    rules = {}
    for intent in ALL_INTENTS:
        enabled = intent.value == "add_category_label" and threshold >= 0.0
        rules[intent.value] = {
            "enabled": enabled,
            "min_model_confidence": threshold if enabled else 1.0,
            "require_evidence": True,
            "observed_precision": (
                best_metrics["auto_action_precision"] if enabled else None
            ),
            "coverage": (
                best_metrics["automation_coverage"] if enabled else None
            ),
            "sample_count": best_metrics["auto_execute_count"] if enabled else 0,
            "allow_auto": enabled,
        }

    data = {
        "schema_version": "1.0",
        "policy_version": "2026-08-17-frozen-from-dev",
        "created_at": "2026-08-17T00:00:00Z",
        "source_dataset_hash": "",
        "model_name": "deterministic-heuristic",
        "prompt_version": "n/a",
        "rules": rules,
        "threshold_scan": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"冻结策略写入: {out_path}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="在 DEV 上选择并冻结 policy 阈值")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("eval/automation/label_ground_truth_dev.jsonl"),
    )
    parser.add_argument("--out", type=Path, default=Path("eval/automation/policy.frozen.json"))
    args = parser.parse_args()

    freeze_best(args.dataset, args.out)


if __name__ == "__main__":
    main()
