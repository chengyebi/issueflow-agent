import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.agent import IssueAgentRequest, run_issue_agent
from app.github_client import (
    add_issue_label,
    post_issue_comment,
)

DATABASE_URL = os.environ["DATABASE_URL"]

def save_completed_run_and_create_review(
    agent_run_id: int,
    result: dict,
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
                    error_message = NULL
                WHERE id = %s;
                """,
                (Jsonb(result), agent_run_id),
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
                    result_json = NULL
                WHERE id = %s;
                """,
                (agent_run_id,),
            )

    # 第二步：根据数据库中的 Issue 创建 Agent 输入
    issue = IssueAgentRequest(
        repo=row["repo"],
        issue_number=row["issue_number"],
        title=row["issue_title"],
        body=row["issue_body"] or "",
    )

    try:
        # 第三步：调用 LangGraph Agent
        response = run_issue_agent(issue)

        # Pydantic 对象转成普通字典
        result = response.model_dump(mode="json")

        # 第四步：成功后保存结果
        save_completed_run_and_create_review(
        agent_run_id=agent_run_id,
        result=result,
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
                        error_message = %s
                    WHERE id = %s;
                    """,
                    (
                        str(exc),
                        agent_run_id,
                    ),
                )

        # 必须继续抛出，让 RQ 知道这个任务失败了
        raise

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
                or command["command_status"] != "approved"
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
                    error_message = NULL
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
                        error_message = NULL
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
                        error_message = %s
                    WHERE id = %s
                      AND status = 'executing';
                    """,
                    (
                        str(exc),
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
                  AND status = 'approved'
                ORDER BY id;
                """,
                (review_task_id,),
            )

            command_ids = [
                row["id"]
                for row in cur.fetchall()
            ]

    results = []

    for command_id in command_ids:
        try:
            results.append(
                process_github_command(command_id)
            )
        except Exception as exc:
            results.append(
                {
                    "command_id": command_id,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    return {
        "review_task_id": review_task_id,
        "commands": results,
    }
