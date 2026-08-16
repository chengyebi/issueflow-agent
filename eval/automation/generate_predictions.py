"""Stage 1：从 ground-truth 数据集生成预测 artifact。

原则（P0-1 / P0-2）：
- 预测只使用 title/body 等模型输入，绝不允许读取 case["category"]。
- true_category 只存在输出 artifact 中，供 Stage 2 比较，不参与预测。
- 确定性启发式 runner 无法产生有意义的 calibration confidence：
  固定 raw_confidence = 1.0，并明确 runner_type="heuristic_smoke"，
  该 artifact 只能用于测试评测框架，不允许作为 production policy freeze 的依据。
- 若要 production 阈值，必须运行与生产一致的 predictor（--runner production_llm），
  并保存 model_name / prompt_version / input_hash；当前阶段不做付费调用。

输出 predictions.jsonl，每条至少：
  repo, issue_number, true_category, predicted_category,
  raw_confidence, action_intent, model_name, prompt_version, input_hash, runner_type
"""

import argparse
import hashlib
import json
from pathlib import Path

from app.automation.models import ActionIntent

from schema import CORE_LABEL_REVERSE

SCHEMA_VERSION = "1.0"

# 确定性启发式标记：不能用于 production threshold。
HEURISTIC_SMOKE_RUNNER = "heuristic_smoke"
HEURISTIC_MODEL_NAME = "deterministic-keyword-heuristic-v1"


def _heuristic_category(title: str, body: str) -> str:
    """只基于输入文本的确定性关键词分类，与 ground truth 完全无关。"""
    text = (title + "\n" + body).lower()
    bug_markers = ("bug", "crash", "error", "exception", "failed", "失败", "异常", "崩溃")
    question_markers = ("how do i", "how to", "how can", "what is", "为什么", "怎么", "请问")
    documentation_markers = ("docs", "documentation", "document", "readme", "文档")
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


def _input_hash(title: str, body: str) -> str:
    payload = (title + "\n" + body).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _map_to_label(category: str) -> str | None:
    """预测分类到 GitHub label（category -> label 方向）。"""
    return CORE_LABEL_REVERSE.get(category)


def generate_predictions(dataset_path: Path, out_path: Path) -> dict:
    cases = []
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))

    predictions = []
    for case in cases:
        title = case.get("title", "")
        body = case.get("body", "")
        # P0-1：预测绝不读取 case["category"]。
        predicted_category = _heuristic_category(title, body)
        label = _map_to_label(predicted_category)
        action_intent = (
            ActionIntent.ADD_CATEGORY_LABEL.value if label else None
        )
        predictions.append(
            {
                "repo": case["repo"],
                "issue_number": case["issue_number"],
                "true_category": case["category"],
                "predicted_category": predicted_category,
                "predicted_label": label,
                "raw_confidence": 1.0,  # heuristic_smoke：固定，非校准值
                "action_intent": action_intent,
                "model_name": HEURISTIC_MODEL_NAME,
                "prompt_version": "n/a",
                "input_hash": _input_hash(title, body),
                "runner_type": HEURISTIC_SMOKE_RUNNER,
            }
        )

    # artifact 整体 SHA-256：基于稳定排序后的全部记录。
    artifact_hash = hashlib.sha256(
        json.dumps(predictions, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    return {
        "prediction_count": len(predictions),
        "prediction_artifact_hash": artifact_hash,
        "runner_type": HEURISTIC_SMOKE_RUNNER,
        "model_name": HEURISTIC_MODEL_NAME,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成预测 artifact（Stage 1）")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("eval/automation/predictions/predictions_dev.jsonl"))
    args = parser.parse_args()

    result = generate_predictions(args.dataset, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
