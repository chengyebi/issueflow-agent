"""P2：production-compatible DEV prediction（Stage 1，真实 LLM）。

- 复用共享 predict_triage（app.agents.triage），模型/prompt/schema 与生产一致。
- 对 dataset v3 DEV 全量运行一次，生成 frozen prediction artifact。
- 每条保存：
    repo, issue_number, input_hash, true_category, expected_label,
    predicted_category, raw_confidence, resolved_label,
    model_name, prompt_version, runner_version, structured_output_success
- structured output 失败不删除样本，记录 failure 并计入覆盖率/可靠性分析。
- 不保存 API key。

用法：
  LLM_API_KEY=... LLM_BASE_URL=... CHAT_MODEL=... python run_production_prediction.py \
    --dataset eval/automation/datasets/label_ground_truth_dev_v3.jsonl \
    --out eval/automation/predictions/predictions_prod_dev_v3.jsonl
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from app.agents.triage import predict_triage
from app.automation.repo_labels import resolve_category_label
from app.core.config import clear_settings_cache, get_settings

RUNNER_VERSION = "prod-v1"
_MAX_BODY_CHARS = 4000  # 截断超长 body，与生产处理对齐（可配置）


def _input_hash(repo: str, issue_number: int, title: str, body: str) -> str:
    payload = f"{repo}#{issue_number}\n{title}\n{body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _truncate_body(body: str) -> str:
    if len(body) <= _MAX_BODY_CHARS:
        return body
    return body[:_MAX_BODY_CHARS] + "\n...[truncated]"


def run_production_prediction(dataset_path: Path, out_path: Path) -> dict:
    settings = get_settings()
    model_name = settings.chat_model
    prompt_version = settings.prompt_version

    cases = []
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))

    predictions = []
    failures = 0
    structured_failures = 0
    started = time.perf_counter()

    for idx, case in enumerate(cases):
        repo = case["repo"]
        issue_number = case["issue_number"]
        title = case.get("title", "")
        body = _truncate_body(case.get("body", ""))
        true_category = case["category"]
        expected_label = case["expected_label"]

        structured_success = True
        try:
            triage = predict_triage(repo, issue_number, title, body)
            predicted_category = triage.category
            raw_confidence = triage.confidence
        except Exception as exc:
            # 失败不删除样本：记录 failure。
            failures += 1
            structured_success = False
            predicted_category = None
            raw_confidence = None
            structured_failures += 1

        resolved_label = (
            resolve_category_label(repo, predicted_category)
            if predicted_category is not None
            else None
        )

        predictions.append(
            {
                "repo": repo,
                "issue_number": issue_number,
                "input_hash": _input_hash(repo, issue_number, title, case.get("body", "")),
                "true_category": true_category,
                "expected_label": expected_label,
                "predicted_category": predicted_category,
                "raw_confidence": raw_confidence,
                "resolved_label": resolved_label,
                "model_name": model_name,
                "prompt_version": prompt_version,
                "runner_version": RUNNER_VERSION,
                "structured_output_success": structured_success,
            }
        )

        if (idx + 1) % 200 == 0:
            print(f"processed {idx + 1}/{len(cases)}")

    elapsed = time.perf_counter() - started

    artifact_hash = hashlib.sha256(
        json.dumps(predictions, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    return {
        "prediction_count": len(predictions),
        "failure_count": failures,
        "structured_output_failure_count": structured_failures,
        "prediction_artifact_hash": artifact_hash,
        "model_name": model_name,
        "prompt_version": prompt_version,
        "runner_version": RUNNER_VERSION,
        "elapsed_seconds": round(elapsed, 1),
        "out_path": str(out_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="P2 production-compatible prediction")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("eval/automation/datasets/label_ground_truth_dev_v3.jsonl"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval/automation/predictions/predictions_prod_dev_v3.jsonl"),
    )
    parser.add_argument("--max-body-chars", type=int, default=4000)
    args = parser.parse_args()

    global _MAX_BODY_CHARS
    _MAX_BODY_CHARS = args.max_body_chars
    clear_settings_cache()
    result = run_production_prediction(args.dataset, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
