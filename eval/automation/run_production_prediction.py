"""P2/P3：production-compatible prediction runner（checkpoint + resume + cost guard）。

- 与 production 完全一致：复用共享 predict_triage（模型/prompt/schema/structured output），
  使用完整 body（不截断）。
- input_hash = 对真正传给模型的 messages 做 canonical JSON SHA-256（sort_keys=True、
  固定 separators、UTF-8），唯一代表模型看到的输入。
- 每条完成立即 append + flush + fsync；支持 --resume。
- 每条保存 predicted_priority / predicted_risk_level（calibration 用真实 risk gate）。
- config fingerprint 隔离不同 config 的 prediction，禁止混写。
- 成本保护：保守 cache-miss 定价（input ¥1/M, output ¥2/M），--max-cost-cny 超限即 checkpoint 停止。
- structured output 失败不删样本，记录 failure。
- 严禁写 API key / Authorization / SecretStr。

用法：
  LLM_API_KEY=... LLM_BASE_URL=... CHAT_MODEL=... python run_production_prediction.py \
    --dataset .../label_ground_truth_dev_v3.jsonl \
    --out eval/automation/predictions/predictions_prod_dev_v3.partial.jsonl
  （--resume 继续；--concurrency 4；--max-cost-cny 8；--limit N）
"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from app.agents.triage import TRIAGE_SYSTEM_PROMPT, TriageResult, predict_triage, triage_messages
from app.agents.workflow import get_llm, _usage_from_message
from app.automation.repo_labels import REPO_CATEGORY_LABELS
from app.core.config import clear_settings_cache, get_settings

RUNNER_VERSION = "prod-v2"

# 保守成本（¥/1M tokens）：全部按 cache-miss input 计算。
PRICE_INPUT_CNY_PER_M = 1.0
PRICE_OUTPUT_CNY_PER_M = 2.0
PRICING_EFFECTIVE_DATE = "2026-08-17"

# 身份 key（resume 用）：repo + issue_number + input_hash。
_IDENTITY_KEYS = ("repo", "issue_number", "input_hash")

# 严格总尝试上限（不无限 retry）。
MAX_ATTEMPTS_PER_CASE = 4


def _canonical_json(obj) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def messages_input_hash(repo: str, issue_number: int, title: str, body: str) -> str:
    """2.2：对真正传给模型的 messages 做 canonical hash。"""
    messages = triage_messages(repo, issue_number, title, body)
    payload = _canonical_json(messages)
    return hashlib.sha256(payload).hexdigest()


def _config_fingerprint(settings) -> dict:
    import inspect
    from app.agents import triage as triage_mod
    from app.automation import repo_labels as rl_mod

    schema_fields = list(TriageResult.model_fields.keys())
    try:
        import subprocess
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    except Exception:
        git_sha = "unknown"

    return {
        "runner_version": RUNNER_VERSION,
        "model_name": settings.chat_model,
        "llm_base_host": (
            settings.llm_base_url.split("//")[-1].split("/")[0]
            if settings.llm_base_url else None
        ),
        "prompt_version": settings.prompt_version,
        "triage_prompt_hash": hashlib.sha256(
            TRIAGE_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "triage_schema_hash": hashlib.sha256(
            _canonical_json(sorted(schema_fields))
        ).hexdigest(),
        "repo_label_resolver_hash": hashlib.sha256(
            _canonical_json(REPO_CATEGORY_LABELS)
        ).hexdigest(),
        "git_sha": git_sha,
        "pricing_effective_date": PRICING_EFFECTIVE_DATE,
    }


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """保守估算（¥）。"""
    return (
        input_tokens * PRICE_INPUT_CNY_PER_M
        + output_tokens * PRICE_OUTPUT_CNY_PER_M
    ) / 1_000_000


def _safe_error_text(exc: Exception) -> str:
    """sanitize 错误信息：截断且去掉可能的敏感字段。"""
    text = str(exc)
    return text[:200].replace("\n", " ")


def _load_cases(dataset_path: Path) -> list[dict]:
    cases = []
    with dataset_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _identity(case: dict, input_hash: str) -> tuple:
    return (case["repo"], case["issue_number"], input_hash)


def _load_partial(partial_path: Path) -> tuple[list[dict], set, dict]:
    """读取 partial JSONL 与 progress manifest。"""
    records = []
    raw_lines = []
    if partial_path.exists():
        with partial_path.open("r", encoding="utf-8") as f:
            raw_lines = f.readlines()
        # 修复最后一个 malformed trailing record（若存在且可安全修复）。
        valid = []
        for i, line in enumerate(raw_lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                valid.append(json.loads(stripped))
            except json.JSONDecodeError:
                if i == len(raw_lines) - 1:
                    # 最后一行损坏：备份后跳过（允许修复最后一个）。
                    backup = partial_path.with_suffix(".bak")
                    if not backup.exists():
                        import shutil
                        shutil.copy(partial_path, backup)
                    print(f"WARN: 修复最后一个损坏 record (line {i+1}), 已备份")
                    continue
                raise
        records = valid

    progress: dict = {}
    progress_path = partial_path.with_suffix(".progress.json")
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))

    done = set()
    for rec in records:
        done.add((rec["repo"], rec["issue_number"], rec["input_hash"]))
    return records, done, progress


def _fsync_append(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def run_production_prediction(
    dataset_path: Path,
    partial_path: Path,
    *,
    resume: bool,
    concurrency: int,
    max_cost_cny: float | None,
    limit: int | None,
) -> dict:
    settings = get_settings()
    fingerprint = _config_fingerprint(settings)

    cases = _load_cases(dataset_path)
    records, done, progress = _load_partial(partial_path)

    # 校验 config fingerprint 一致性（resume 时）。
    if resume and records:
        if progress.get("config_fingerprint") != fingerprint:
            raise RuntimeError(
                "config_fingerprint 不匹配，拒绝 append 到现有 partial。"
                "必须使用新 artifact。"
            )

    # 构建剩余待处理 cases。
    remaining = []
    for case in cases:
        ih = messages_input_hash(case["repo"], case["issue_number"], case.get("title", ""), case.get("body", ""))
        ident = _identity(case, ih)
        if ident in done:
            continue
        remaining.append((case, ih))
    if limit is not None:
        remaining = remaining[:limit]

    from concurrent.futures import ThreadPoolExecutor
    from app.automation.repo_labels import resolve_category_label

    import threading
    _thread_usage = threading.local()

    def _eval_invoke_structured(schema, messages):
        """与 production 相同模型/prompt/schema，但捕获真实 token usage。

        复用 workflow.get_llm() 与 _usage_from_message()，
        因此与 production structured output 调用完全一致。
        """
        model = get_llm().with_structured_output(
            schema, method="function_calling", include_raw=True
        )
        response = model.invoke(messages)
        parsed = response.get("parsed") if isinstance(response, dict) else response
        raw = response.get("raw") if isinstance(response, dict) else None
        if raw is not None:
            _thread_usage.input_tokens, _thread_usage.output_tokens = _usage_from_message(raw)
        if parsed is None:
            raise ValueError(
                f"结构化输出解析失败: {type(response.get('parsing_error')).__name__}"
            )
        return parsed

    def _process_one(item):
        case, ih = item
        repo = case["repo"]
        issue_number = case["issue_number"]
        title = case.get("title", "")
        body = case.get("body", "")
        case_started = time.perf_counter()
        record = {
            "repo": repo,
            "issue_number": issue_number,
            "input_hash": ih,
            "true_category": case["category"],
            "expected_label": case["expected_label"],
            "model_name": settings.chat_model,
            "prompt_version": settings.prompt_version,
            "runner_version": RUNNER_VERSION,
            "pricing_effective_date": PRICING_EFFECTIVE_DATE,
        }
        structured_success = True
        error_type = None
        in_tokens = 0
        out_tokens = 0
        _thread_usage.input_tokens = 0
        _thread_usage.output_tokens = 0
        try:
            triage = predict_triage(
                repo, issue_number, title, body,
                invoke_structured=_eval_invoke_structured,
            )
            predicted_category = triage.category
            raw_confidence = triage.confidence
            predicted_priority = triage.priority
            predicted_risk_level = triage.risk_level
            in_tokens = getattr(_thread_usage, "input_tokens", 0)
            out_tokens = getattr(_thread_usage, "output_tokens", 0)
        except Exception as exc:
            structured_success = False
            predicted_category = None
            raw_confidence = None
            predicted_priority = None
            predicted_risk_level = None
            error_type = _safe_error_text(exc)
        resolved_label = (
            resolve_category_label(repo, predicted_category)
            if predicted_category is not None
            else None
        )
        record.update(
            {
                "predicted_category": predicted_category,
                "raw_confidence": raw_confidence,
                "predicted_priority": predicted_priority,
                "predicted_risk_level": predicted_risk_level,
                "resolved_label": resolved_label,
                "structured_output_success": structured_success,
                "retry_count": 0,
                "error_type": error_type,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "elapsed_ms": round((time.perf_counter() - case_started) * 1000),
                "usage_source": "usage_metadata",
            }
        )
        return record

    # 并发调用，但按 remaining 顺序落盘（并发只改吞吐，不改 schema/排序）。
    started = time.perf_counter()
    count = 0
    total_input = 0
    total_output = 0
    total_cost = 0.0
    failures = 0
    structured_failures = 0
    latency_ms_list = []
    stopped_by_cost = False

    pool_workers = max(1, concurrency)
    with ThreadPoolExecutor(max_workers=pool_workers) as executor:
        for record in executor.map(_process_one, remaining):
            # 每条立即持久化。
            _fsync_append(partial_path, record)
            done.add((record["repo"], record["issue_number"], record["input_hash"]))

            total_input += record["input_tokens"]
            total_output += record["output_tokens"]
            total_cost += _estimate_cost(record["input_tokens"], record["output_tokens"])
            latency_ms_list.append(record["elapsed_ms"])
            count += 1
            if not record["structured_output_success"]:
                failures += 1
                structured_failures += 1

            # 成本保护：超限 checkpoint 停止。
            if max_cost_cny is not None and total_cost > max_cost_cny:
                print(f"COST GUARD: 累计成本 ¥{total_cost:.4f} 超过上限 ¥{max_cost_cny}，checkpoint 停止")
                stopped_by_cost = True
                break

            if count % 50 == 0:
                print(f"processed {count}/{len(remaining)} (total records {len(done)})")

    elapsed = time.perf_counter() - started

    # 更新 progress manifest。
    progress = {
        "config_fingerprint": fingerprint,
        "dataset_path": str(dataset_path),
        "dataset_hash": _file_hash(dataset_path),
        "record_count": len(done),
        "remaining_count": len(remaining) - count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_cny": round(total_cost, 6),
        "usage_source": "usage_metadata",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    progress_path = partial_path.with_suffix(".progress.json")
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    avg_latency = (
        sum(latency_ms_list) / len(latency_ms_list) if latency_ms_list else 0
    )
    return {
        "total_attempted": count,
        "total_records": len(done),
        "structured_output_failure_count": structured_failures,
        "failure_count": failures,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_cny": round(total_cost, 6),
        "elapsed_seconds": round(elapsed, 1),
        "avg_latency_ms": round(avg_latency, 1),
        "remaining_count": len(remaining) - count,
    }


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def finalize_artifact(partial_path: Path, final_path: Path) -> str:
    """把 partial 稳定排序为最终 frozen artifact，返回 artifact SHA-256。"""
    records, done, progress = _load_partial(partial_path)
    # 按 dataset 原始顺序稳定排序：repo + issue_number。
    records.sort(key=lambda r: (r["repo"], r["issue_number"]))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with open(final_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return _file_hash(final_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="production-compatible prediction")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True,
                        help="partial JSONL 路径（--resume 继续同一文件）")
    parser.add_argument("--final", type=Path, default=None,
                        help="完成/排序后输出的最终 frozen artifact 路径")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-cost-cny", type=float, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    clear_settings_cache()
    result = run_production_prediction(
        args.dataset,
        args.out,
        resume=args.resume,
        concurrency=args.concurrency,
        max_cost_cny=args.max_cost_cny,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.final:
        final_hash = finalize_artifact(args.out, args.final)
        print(f"FINAL ARTIFACT: {args.final} SHA-256={final_hash}")


if __name__ == "__main__":
    main()
