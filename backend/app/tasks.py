import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.agent import IssueAgentRequest, run_issue_agent


DATABASE_URL = os.environ["DATABASE_URL"]

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
                    (
                        Jsonb(result),
                        agent_run_id,
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
