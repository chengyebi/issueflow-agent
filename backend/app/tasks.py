import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.agent import IssueAgentRequest, run_issue_agent
from app.core.config import get_settings
from app.core.sanitization import sanitize_error_message
from app.core.tracing import TraceSession
from app.github_client import (
    add_issue_label,
    post_issue_comment,
)
from app.services.traces import DatabaseTraceRecorder

DATABASE_URL = get_settings().database_url

def save_completed_run_and_create_review(
    agent_run_id: int,
    result: dict,
    duration_ms: int,
    trace: TraceSession,
    estimated_cost_usd: float | None,
) -> None:
    actions = result.get("proposed_actions", [])

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_runs
                SET
                    status = 'completed',
                    finished_at = NOW(),
                    result_json = %s,
                    error_message = NULL,
                    error_type = NULL,
                    duration_ms = %s,
                    input_tokens = %s,
                    output_tokens = %s,
                    structured_output_success = %s,
                    estimated_cost_usd = %s
                WHERE id = %s;
                """,
                (
                    Jsonb(result),
                    duration_ms,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.structured_output_success,
                    estimated_cost_usd,
                    agent_run_id,
                ),
            )

            cur.execute(
                """
                INSERT INTO review_tasks (agent_run_id)
                VALUES (%s)
                ON CONFLICT (agent_run_id)
                DO UPDATE SET
                    agent_run_id = EXCLUDED.agent_run_id
                RETURNING id;
                """,
                (agent_run_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise RuntimeError("创建审核任务后没有返回 ID")

            review_task_id = row[0]

            for index, action in enumerate(actions):
                command_type = action["type"]
                command_value = action["value"]

                idempotency_key = (
                    f"agent-run:{agent_run_id}:"
                    f"action:{index}:{command_type}"
                )

                cur.execute(
                    """
                    INSERT INTO github_commands (
                        review_task_id,
                        command_type,
                        payload,
                        idempotency_key
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (idempotency_key)
                    DO NOTHING;
                    """,
                    (
                        review_task_id,
                        command_type,
                        Jsonb({"value": command_value}),
                        idempotency_key,
                    ),
                )

def process_issue_agent_run(agent_run_id: int) -> dict:
    # 第一步：查询任务对应的 Issue，并把任务标记为 running
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    ar.id AS agent_run_id,
                    ar.trace_id,
                    ie.repo,
                    ie.issue_number,
                    ie.issue_title,
                    ie.issue_body
                FROM agent_runs ar
                JOIN issue_events ie
                    ON ie.id = ar.issue_event_id
                WHERE ar.id = %s;
                """,
                (agent_run_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise ValueError(
                    f"Agent Run 不存在: {agent_run_id}"
                )

            cur.execute(
                """
                UPDATE agent_runs
                SET
                    status = 'running',
                    started_at = NOW(),
                    finished_at = NULL,
                    error_message = NULL,
                    result_json = NULL,
                    model_name = %s,
                    prompt_version = %s,
                    agent_version = %s,
                    agent_mode = %s,
                    retry_count = retry_count + CASE
                        WHEN started_at IS NULL THEN 0 ELSE 1 END
                WHERE id = %s;
                """,
                (
                    get_settings().chat_model,
                    get_settings().prompt_version,
                    get_settings().agent_version,
                    get_settings().agent_mode,
                    agent_run_id,
                ),
            )

    # 第二步：根据数据库中的 Issue 创建 Agent 输入
    issue = IssueAgentRequest(
        repo=row["repo"],
        issue_number=row["issue_number"],
        title=row["issue_title"],
        body=row["issue_body"] or "",
    )

    trace = TraceSession(
        trace_id=str(row["trace_id"]),
        recorder=DatabaseTraceRecorder(agent_run_id, str(row["trace_id"])),
    )
    started = time.perf_counter()
    try:
        # 第三步：调用 LangGraph Agent
        response = run_issue_agent(issue, trace=trace)

        # Pydantic 对象转成普通字典
        result = response.model_dump(mode="json")

        # 第四步：成功后保存结果
        save_completed_run_and_create_review(
            agent_run_id=agent_run_id,
            result=result,
            duration_ms=round((time.perf_counter() - started) * 1000),
            trace=trace,
            estimated_cost_usd=_estimate_cost(
                trace.input_tokens, trace.output_tokens
            ),
        )

        return result

    except Exception as exc:
        # 第四步：失败后保存错误信息
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_runs
                    SET
                        status = 'failed',
                        finished_at = NOW(),
                        error_message = %s,
                        error_type = %s,
                        duration_ms = %s,
                        input_tokens = %s,
                        output_tokens = %s,
                        structured_output_success = %s,
                        estimated_cost_usd = %s
                    WHERE id = %s;
                    """,
                    (
                        sanitize_error_message(exc),
                        type(exc).__name__,
                        round((time.perf_counter() - started) * 1000),
                        trace.input_tokens,
                        trace.output_tokens,
                        False,
                        _estimate_cost(trace.input_tokens, trace.output_tokens),
                        agent_run_id,
                    ),
                )

        # 必须继续抛出，让 RQ 知道这个任务失败了
        raise


def _estimate_cost(input_tokens: int, output_tokens: int) -> float | None:
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

def process_github_command(command_id: int) -> dict:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    gc.id,
                    gc.command_type,
                    gc.payload,
                    gc.status AS command_status,
                    rt.status AS review_status,
                    ie.repo,
                    ie.issue_number
                FROM github_commands gc
                JOIN review_tasks rt
                    ON rt.id = gc.review_task_id
                JOIN agent_runs ar
                    ON ar.id = rt.agent_run_id
                JOIN issue_events ie
                    ON ie.id = ar.issue_event_id
                WHERE gc.id = %s
                FOR UPDATE OF gc;
                """,
                (command_id,),
            )

            command = cur.fetchone()

            if command is None:
                raise ValueError(
                    f"GitHub Command 不存在: {command_id}"
                )

            if (
                command["review_status"] != "approved"
                or command["command_status"] not in {"approved", "failed"}
            ):
                return {
                    "command_id": command_id,
                    "status": command["command_status"],
                    "skipped": True,
                }

            cur.execute(
                """
                UPDATE github_commands
                SET
                    status = 'executing',
                    updated_at = NOW(),
                    error_message = NULL,
                    error_type = NULL,
                    retry_safe = FALSE
                WHERE id = %s;
                """,
                (command_id,),
            )

    try:
        payload = command["payload"] or {}
        value = payload.get("value")

        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "GitHub Command payload.value 必须是非空字符串"
            )

        if command["command_type"] == "add_label":
            labels = add_issue_label(
                repo=command["repo"],
                issue_number=command["issue_number"],
                label=value,
            )

            result = {
                "label": value,
                "labels": [
                    item.get("name")
                    for item in labels
                ],
            }

        elif command["command_type"] == "post_comment":
            comment = post_issue_comment(
                repo=command["repo"],
                issue_number=command["issue_number"],
                body=value,
            )

            result = {
                "comment_id": comment.get("id"),
                "comment_url": comment.get("html_url"),
            }

        else:
            raise ValueError(
                "不支持的 GitHub Command 类型: "
                f"{command['command_type']}"
            )

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE github_commands
                    SET
                        status = 'executed',
                        updated_at = NOW(),
                        executed_at = NOW(),
                        error_message = NULL,
                        error_type = NULL,
                        retry_safe = FALSE
                    WHERE id = %s
                      AND status = 'executing';
                    """,
                    (command_id,),
                )

        return {
            "command_id": command_id,
            "status": "executed",
            "result": result,
        }

    except Exception as exc:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE github_commands
                    SET
                        status = 'failed',
                        updated_at = NOW(),
                        error_message = %s,
                        error_type = %s,
                        retry_safe = %s
                    WHERE id = %s
                      AND status = 'executing';
                    """,
                    (
                        sanitize_error_message(exc),
                        type(exc).__name__,
                        bool(getattr(exc, "retry_safe", False)),
                        command_id,
                    ),
                )

        raise


def process_review_commands(
    review_task_id: int,
) -> dict:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id
                FROM github_commands
                WHERE review_task_id = %s
                  AND (status = 'approved' OR (status = 'failed' AND retry_safe))
                ORDER BY id;
                """,
                (review_task_id,),
            )

            command_ids = [
                row["id"]
                for row in cur.fetchall()
            ]

    results = []

    retryable_failures = []
    for command_id in command_ids:
        try:
            results.append(
                process_github_command(command_id)
            )
        except Exception as exc:
            failure = {
                "command_id": command_id,
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            results.append(failure)
            if bool(getattr(exc, "retry_safe", False)):
                retryable_failures.append(failure)

    if retryable_failures:
        raise RuntimeError(
            f"{len(retryable_failures)} 个 GitHub Command 执行失败，将按有限策略重试"
        )

    return {
        "review_task_id": review_task_id,
        "commands": results,
    }
