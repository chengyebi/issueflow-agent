import argparse
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.agents.workflow import IssueAgentRequest, run_issue_agent
from app.core.config import get_settings
from app.core.tracing import TraceSession
from app.db.connection import connect
from app.eval.metrics import calculate_metrics
from app.eval.schema import EvalCase, EvalPrediction


def load_cases(path: Path) -> list[EvalCase]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(EvalCase.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number} 不符合 Eval Schema") from exc
    if not cases:
        raise ValueError("评测数据集不能为空")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("评测 case id 必须唯一")
    return cases


def heuristic_prediction(case: EvalCase) -> EvalPrediction:
    started = time.perf_counter()
    text = f"{case.input.title}\n{case.input.body}".lower()
    security_terms = ("漏洞", "密钥", "token", "认证绕过", "rce", "隐私")
    if any(term in text for term in security_terms):
        risk = "high"
    else:
        risk = "low"
    if any(term in text for term in ("崩溃", "报错", "失败", "bug", "500")):
        category = "bug"
    elif any(term in text for term in ("功能", "建议", "feature")):
        category = "feature"
    elif any(term in text for term in ("文档", "readme", "documentation")):
        category = "documentation"
    elif "?" in text or "如何" in text:
        category = "question"
    else:
        category = "other"
    priority = "critical" if risk == "high" else "medium"
    return EvalPrediction(
        case_id=case.id,
        category=category,
        priority=priority,
        risk_level=risk,
        structured_output_success=True,
        agent_success=True,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


def live_prediction(case: EvalCase) -> EvalPrediction:
    started = time.perf_counter()
    trace = TraceSession(trace_id=str(uuid4()))
    try:
        result = run_issue_agent(
            IssueAgentRequest.model_validate(case.input.model_dump()), trace=trace
        )
        return EvalPrediction(
            case_id=case.id,
            category=result.category,
            priority=result.priority,
            risk_level=result.risk_level,
            structured_output_success=trace.structured_output_success,
            agent_success=True,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=trace.input_tokens,
            output_tokens=trace.output_tokens,
            estimated_cost_usd=estimate_cost(trace.input_tokens, trace.output_tokens),
        )
    except Exception as exc:
        return EvalPrediction(
            case_id=case.id,
            structured_output_success=False,
            agent_success=False,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=trace.input_tokens,
            output_tokens=trace.output_tokens,
            estimated_cost_usd=estimate_cost(trace.input_tokens, trace.output_tokens),
            error_type=type(exc).__name__,
        )


def estimate_cost(input_tokens: int, output_tokens: int) -> float | None:
    settings = get_settings()
    if (
        settings.llm_input_cost_per_million_usd is None
        or settings.llm_output_cost_per_million_usd is None
    ):
        return None
    return (
        input_tokens * settings.llm_input_cost_per_million_usd
        + output_tokens * settings.llm_output_cost_per_million_usd
    ) / 1_000_000


def persist_report(report: dict) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eval_reports (
                eval_id, dataset_name, dataset_hash, runner_type, model_name,
                prompt_version, agent_version, agent_mode, case_count,
                metrics, report_json, publishable_model_score
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                report["eval_id"],
                report["dataset_name"],
                report["dataset_hash"],
                report["runner_type"],
                report["model_name"],
                report["prompt_version"],
                report["agent_version"],
                report["agent_mode"],
                report["metrics"]["case_count"],
                Jsonb(report["metrics"]),
                Jsonb(report),
                report["publishable_model_score"],
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="批量运行 IssueFlow 离线评测")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runner", choices=("heuristic", "live"), default="heuristic")
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    if args.runner == "live" and not args.allow_external:
        parser.error("live runner 会调用外部模型，必须显式提供 --allow-external")

    raw_dataset = args.dataset.read_bytes()
    cases = load_cases(args.dataset)
    runner = live_prediction if args.runner == "live" else heuristic_prediction
    predictions = [runner(case) for case in cases]
    settings = get_settings()
    report = {
        "schema_version": "1.0",
        "eval_id": str(uuid4()),
        "dataset_name": args.dataset.name,
        "dataset_hash": hashlib.sha256(raw_dataset).hexdigest(),
        "runner_type": args.runner,
        "model_name": settings.chat_model if args.runner == "live" else None,
        "prompt_version": settings.prompt_version,
        "agent_version": settings.agent_version,
        "agent_mode": settings.agent_mode,
        "publishable_model_score": args.runner == "live"
        and all(case.metadata.source != "synthetic_example" for case in cases),
        "metrics": calculate_metrics(cases, predictions),
        "cases": [prediction.model_dump(mode="json") for prediction in predictions],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.persist:
        persist_report(report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
