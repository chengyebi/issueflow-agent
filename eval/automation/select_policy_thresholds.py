"""在 DEV 上扫描阈值并冻结 policy artifact（Stage 2 的阈值选择）。

原则（P0-6 / P0-7）：
- 只读取一份冻结的 prediction artifact（predictions.jsonl），
  在完全相同的 prediction 上扫描所有 threshold，不重复调用模型。
- 不要求 error_count == 0（小样本下 0 error 不等于可靠）；
  同时输出 sample_count 与 precision Wilson CI lower bound。
- 不自行声称行业阈值。
- 正式 freeze 时：
    source_dataset_hash 必须来自 dataset manifest 的 SHA-256（不能为空）；
    prediction_artifact_hash / model_name / prompt_version / created_at / sample_count
    必须写入 policy artifact；
    enabled intent 必须有 observed_precision（非 null）、sample_count > 0、allow_auto=true。
- heuristic_smoke 的 prediction artifact 不能用于 production freeze。

策略选择：不自动选择单个阈值输出为“正式策略”，而是输出完整 precision-coverage
曲线，由人工在满足产品精度门槛的前提下选择。默认输出 frozen 候选 artifact，
但 marked 为 heuristic_smoke 来源，不能直接用于 enforce。
"""

import argparse
import json
from pathlib import Path

from app.automation.models import ActionIntent
from app.automation.policy_loader import ALL_INTENTS, POLICY_SCHEMA_VERSION
from run_automation_eval import _wilson_ci, run_eval

THRESHOLD_CANDIDATES = [0.0, 0.70, 0.80, 0.90, 0.95, 0.99]

# 用于 smoke：临时 policy 只放开 add_category_label。
SMOKE_ALLOWED_INTENT = ActionIntent.ADD_CATEGORY_LABEL.value


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
            "allow_auto": intent.value == SMOKE_ALLOWED_INTENT,
        }
    data = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_version": f"threshold-scan-{threshold}",
        "created_at": "2026-08-17T00:00:00Z",
        "source_dataset_hash": "scan-temporary",
        "rules": rules,
    }
    tmp = Path(tempfile.gettempdir()) / f"policy-t{threshold}.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    return tmp


def _read_dataset_hash(manifest_path: Path) -> str:
    """从 dataset manifest 读取真实 SHA-256。"""
    if not manifest_path.exists():
        return ""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("dataset_hash", "")


def scan_thresholds(predictions_path: Path, dataset_manifest: Path) -> list[dict]:
    """在同一份 prediction artifact 上扫描全部候选阈值。"""
    results = []
    for threshold in THRESHOLD_CANDIDATES:
        policy_file = _temporary_policy(threshold)
        report = run_eval(predictions_path, policy_file)
        auto_total = report["auto_execute_count"]
        correct = report["auto_execute_count"] - report["error_auto_execute_count"]
        lower, upper = _wilson_ci(correct, auto_total)
        results.append(
            {
                "threshold": threshold,
                "auto_count": auto_total,
                "coverage": report["automation_coverage"],
                "correct": correct,
                "incorrect": report["error_auto_execute_count"],
                "precision": report["auto_action_precision"],
                "precision_ci_lower": round(lower, 4),
                "precision_ci_upper": round(upper, 4),
                "sample_count": auto_total,
            }
        )
    return results


def freeze_candidate(
    predictions_path: Path,
    dataset_manifest: Path,
    out_path: Path,
    *,
    selected_threshold: float,
) -> dict:
    """从一份 prediction artifact + 真实 dataset hash 生成冻结候选。

    仅当 artifact 是 production-compatible（非 heuristic_smoke）时才允许
    标记为可 enforce 的冻结策略；heuristic_smoke 只能生成候选，不允许 enforce。
    """
    # 读取 prediction artifact 元数据。
    lines = [ln for ln in predictions_path.read_text().splitlines() if ln.strip()]
    predictions = [json.loads(ln) for ln in lines]
    if not predictions:
        raise RuntimeError("prediction artifact 为空")
    runner_type = predictions[0].get("runner_type", "")
    # artifact hash = 文件内容 SHA-256（与 generate_predictions 返回一致）。
    import hashlib

    artifact_hash_payload = predictions_path.read_bytes()
    prediction_artifact_hash = hashlib.sha256(artifact_hash_payload).hexdigest()

    dataset_hash = _read_dataset_hash(dataset_manifest)
    if not dataset_hash:
        raise RuntimeError(
            "dataset manifest 缺失 dataset_hash：不能冻结正式 policy"
        )

    # 在选定阈值上重跑一次得到指标。
    policy_file = _temporary_policy(selected_threshold)
    report = run_eval(predictions_path, policy_file)
    auto_total = report["auto_execute_count"]
    correct = auto_total - report["error_auto_execute_count"]

    # 仅 production-compatible artifact 允许 enforce。
    enforce_allowed = runner_type != "heuristic_smoke" and bool(prediction_artifact_hash)

    rules = {}
    for intent in ALL_INTENTS:
        is_label = intent.value == SMOKE_ALLOWED_INTENT
        rules[intent.value] = {
            "enabled": is_label and enforce_allowed,
            "min_model_confidence": selected_threshold if (is_label and enforce_allowed) else 1.0,
            "require_evidence": True,
            "observed_precision": (
                round(correct / auto_total, 4) if (is_label and enforce_allowed and auto_total) else None
            ),
            "coverage": (
                report["automation_coverage"] if (is_label and enforce_allowed) else None
            ),
            "sample_count": auto_total if (is_label and enforce_allowed) else 0,
            "allow_auto": is_label and enforce_allowed,
        }

    data = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_version": "2026-08-17-frozen-candidate",
        "created_at": "2026-08-17T00:00:00Z",
        "source_dataset_hash": dataset_hash,
        "prediction_artifact_hash": prediction_artifact_hash,
        "model_name": predictions[0].get("model_name"),
        "prompt_version": predictions[0].get("prompt_version"),
        "runner_type": runner_type,
        "sample_count": auto_total,
        "enforce_ready": enforce_allowed,
        "metrics": {
            "by_category": report.get("by_category", {}),
            "by_repo": report.get("by_repo", {}),
            "by_repo_category": report.get("by_repo_category", {}),
            "by_confidence_bucket": report.get("by_confidence_bucket", {}),
        },
        "rules": rules,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描阈值并生成冻结候选")
    parser.add_argument(
        "--predictions",
        type=Path,
        default=Path("eval/automation/predictions/predictions_dev_v2.jsonl"),
    )
    parser.add_argument(
        "--dataset-manifest",
        type=Path,
        default=Path("eval/automation/datasets/label_ground_truth_dev_v2.manifest.json"),
    )
    parser.add_argument(
        "--out", type=Path, default=Path("eval/automation/policy.frozen.json")
    )
    parser.add_argument("--threshold", type=float, default=None,
                        help="人工选定阈值；缺省仅输出扫描曲线")
    args = parser.parse_args()

    curve = scan_thresholds(args.predictions, args.dataset_manifest)
    print(json.dumps({"threshold_curve": curve}, ensure_ascii=False, indent=2))

    if args.threshold is not None:
        frozen = freeze_candidate(
            args.predictions, args.dataset_manifest, args.out,
            selected_threshold=args.threshold,
        )
        print(json.dumps(frozen, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
