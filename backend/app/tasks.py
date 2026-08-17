import time

import psycopg
from psycopg.rows import dict_row

from app.agent import IssueAgentRequest, run_issue_agent
from app.core.config import get_settings
from app.core.sanitization import sanitize_error_message
from app.core.tracing import TraceSession
from app.github_client import (
    add_issue_label,
    post_issue_comment,
)
from app.services.automation_router import save_completed_run_and_route
from app.services.outbox import dispatch_event
from app.services.traces import DatabaseTraceRecorder

DATABASE_URL = get_settings().database_url


def _dispatch_route_outbox(outbox_event_key: str | None) -> None:
    """路由事务提交后，立即尝试派发新建的 Outbox 事件。"""
    if outbox_event_key:
        dispatch_event(outbox_event_key)


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
                    ie.issue_body,
                    COALESCE(hi.labels, '[]'::jsonb) AS labels
                FROM agent_runs ar
                JOIN issue_events ie
                    ON ie.id = ar.issue_event_id
                LEFT JOIN historical_issues hi
                    ON hi.repo = ie.repo AND hi.issue_number = ie.issue_number
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
        labels=list(row["labels"] or []),
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

        # 第四步：成功后保存结果并按 automation mode 路由
        outcome = save_completed_run_and_route(
            agent_run_id=agent_run_id,
            result=result,
            duration_ms=round((time.perf_counter() - started) * 1000),
            trace=trace,
            estimated_cost_usd=_estimate_cost(
                trace.input_tokens, trace.output_tokens
            ),
        )

        # save_completed_run_and_route 已经完成数据库事务提交。
        # AUTO_EXECUTE 如果创建了 github_commands Outbox，
        # 此处立即尝试投递到 RQ；失败时仍由 Outbox 保留恢复能力。
        _dispatch_route_outbox(outcome.outbox_event_key)

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

def is_command_authorized(command: dict) -> bool:
    """判断一个 GitHub Command 是否持有合法授权。

    - policy 授权：review_task_id 必须为空，command 状态为 approved/failed，
      且必须有 policy_version。
    - human 授权：必须关联已 approved 的 review_task，command 状态为 approved/failed。
    """
    source = command.get("authorization_source")

    if source == "policy":
        return (
            command.get("review_task_id") is None
            and command.get("command_status") in {"approved", "failed"}
            and bool(command.get("policy_version"))
        )

    if source == "human":
        return (
            command.get("review_task_id") is not None
            and command.get("review_status") == "approved"
            and command.get("command_status") in {"approved", "failed"}
        )

    return False


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
                    gc.review_task_id,
                    gc.authorization_source,
                    gc.policy_version,
                    rt.status AS review_status,
                    ie.repo,
                    ie.issue_number
                FROM github_commands gc
                LEFT JOIN review_tasks rt
                    ON rt.id = gc.review_task_id
                LEFT JOIN agent_runs ar
                    ON ar.id = gc.agent_run_id
                LEFT JOIN issue_events ie
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

            if not is_command_authorized(command):
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


def process_authorized_commands(agent_run_id: int) -> dict:
    """执行一个 Agent Run 下所有 policy 授权的 GitHub Commands。

    这是自动化路径的统一入口：policy 授权命令在 AUTO_EXECUTE 时写入
    outbox_events（event_type='github_commands'），由该函数消费。
    授权判定复用 process_github_command 内部的 is_command_authorized。
    """
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id
                FROM github_commands
                WHERE agent_run_id = %s
                  AND authorization_source = 'policy'
                  AND (status = 'approved' OR (status = 'failed' AND retry_safe))
                ORDER BY id;
                """,
                (agent_run_id,),
            )
            command_ids = [row["id"] for row in cur.fetchall()]

    results = []
    retryable_failures = []
    for command_id in command_ids:
        try:
            results.append(process_github_command(command_id))
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
        "agent_run_id": agent_run_id,
        "commands": results,
    }


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
