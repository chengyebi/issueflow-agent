from typing import Literal

from psycopg.rows import dict_row

from app.db.connection import connect
from app.services.exceptions import ConflictError, NotFoundError


def list_review_tasks(status: str | None = None) -> list[dict]:
    query = """
        SELECT rt.id AS review_task_id, rt.status AS review_status,
               rt.reviewer, rt.review_note, rt.created_at, rt.reviewed_at,
               ar.id AS agent_run_id, ar.result_json,
               ie.repo, ie.issue_number, ie.issue_title, ie.issue_body
        FROM review_tasks rt
        JOIN agent_runs ar ON ar.id = rt.agent_run_id
        JOIN issue_events ie ON ie.id = ar.issue_event_id
    """
    params: list[str] = []
    if status is not None:
        query += " WHERE rt.status = %s"
        params.append(status)
    query += " ORDER BY rt.id DESC"

    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        reviews = list(cur.fetchall())
        for review in reviews:
            cur.execute(
                """
                SELECT id, command_type, payload, status, idempotency_key,
                       error_type, error_message, retry_safe
                FROM github_commands WHERE review_task_id = %s ORDER BY id
                """,
                (review["review_task_id"],),
            )
            review["commands"] = list(cur.fetchall())
        return reviews


def decide_review_task(
    review_task_id: int,
    decision: Literal["approved", "rejected"],
    reviewer: str,
    review_note: str | None,
) -> dict:
    with connect(row_factory=dict_row) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM review_tasks WHERE id = %s FOR UPDATE",
            (review_task_id,),
        )
        task = cur.fetchone()
        if task is None:
            raise NotFoundError("Review task not found")
        if task["status"] != "pending":
            raise ConflictError(
                f"Review task has already been decided: {task['status']}"
            )

        cur.execute(
            """
            UPDATE review_tasks
            SET status = %s, reviewer = %s, review_note = %s, reviewed_at = NOW()
            WHERE id = %s
            RETURNING id, status, reviewer, review_note, reviewed_at
            """,
            (decision, reviewer, review_note, review_task_id),
        )
        updated_review = cur.fetchone()
        cur.execute(
            """
            UPDATE github_commands SET status = %s, updated_at = NOW()
            WHERE review_task_id = %s AND status = 'proposed'
            RETURNING id
            """,
            (decision, review_task_id),
        )
        command_ids = [row["id"] for row in cur.fetchall()]
        outbox_event_key = None
        if decision == "approved" and command_ids:
            outbox_event_key = f"review-commands:{review_task_id}"
            cur.execute(
                """
                INSERT INTO outbox_events (
                    event_key, event_type, aggregate_id, payload
                ) VALUES (
                    %s, 'review_commands', %s,
                    jsonb_build_object('review_task_id', %s)
                )
                ON CONFLICT (event_key) DO NOTHING
                """,
                (outbox_event_key, review_task_id, review_task_id),
            )
    return {
        "review_task": updated_review,
        "updated_command_ids": command_ids,
        "outbox_event_key": outbox_event_key,
    }
