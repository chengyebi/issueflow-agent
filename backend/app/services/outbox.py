from dataclasses import dataclass
from uuid import uuid4

from psycopg.rows import dict_row

from app.core.config import get_settings
from app.core.sanitization import sanitize_error_message
from app.db.connection import connect


@dataclass(frozen=True)
class DispatchResult:
    event_key: str
    status: str
    rq_job_id: str | None
    recovery_pending: bool


def dispatch_event(event_key: str) -> DispatchResult:
    with connect(row_factory=dict_row) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_key, event_type, aggregate_id, attempts, max_attempts,
                   status, rq_job_id
            FROM outbox_events
            WHERE event_key = %s
            FOR UPDATE SKIP LOCKED
            """,
            (event_key,),
        )
        event = cur.fetchone()
        if event is None:
            return DispatchResult(event_key, "missing", None, False)
        if event["status"] == "dispatched":
            return DispatchResult(event_key, "dispatched", event["rq_job_id"], False)
        if event["status"] == "processing":
            return DispatchResult(event_key, "processing", None, True)
        if event["attempts"] >= event["max_attempts"]:
            return DispatchResult(event_key, "failed", None, False)
        cur.execute(
            """
            UPDATE outbox_events
            SET status = 'processing', attempts = attempts + 1,
                locked_at = NOW(), updated_at = NOW(),
                error_type = NULL, error_message = NULL
            WHERE id = %s
            """,
            (event["id"],),
        )
        attempt = event["attempts"] + 1

    try:
        from app.workers.queue import (
            enqueue_issue_agent_run,
            enqueue_review_commands,
        )

        if event["event_type"] == "agent_run":
            rq_job_id = enqueue_issue_agent_run(event["aggregate_id"])
        elif event["event_type"] == "github_commands":
            from app.workers.queue import enqueue_authorized_commands

            rq_job_id = enqueue_authorized_commands(event["aggregate_id"])
        elif event["event_type"] == "review_commands":
            rq_job_id = enqueue_review_commands(event["aggregate_id"])
        elif event["event_type"] == "issue_index":
            from app.workers.queue import enqueue_issue_embedding

            rq_job_id = enqueue_issue_embedding(event["aggregate_id"])
        else:
            raise ValueError(f"不支持的 Outbox 事件类型: {event['event_type']}")
    except Exception as exc:
        settings = get_settings()
        terminal = attempt >= event["max_attempts"]
        backoff = settings.outbox_base_backoff_seconds * (2 ** max(0, attempt - 1))
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE outbox_events
                SET status = %s, available_at = NOW() + (%s * INTERVAL '1 second'),
                    locked_at = NULL, error_type = %s, error_message = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    "failed" if terminal else "pending",
                    backoff,
                    type(exc).__name__,
                    sanitize_error_message(exc),
                    event["id"],
                ),
            )
        return DispatchResult(
            event_key, "failed" if terminal else "pending", None, not terminal
        )

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE outbox_events
            SET status = 'dispatched', rq_job_id = %s, dispatched_at = NOW(),
                locked_at = NULL, updated_at = NOW()
            WHERE id = %s
            """,
            (rq_job_id, event["id"]),
        )
        if event["event_type"] == "agent_run":
            cur.execute(
                "UPDATE agent_runs SET rq_job_id = %s WHERE id = %s",
                (rq_job_id, event["aggregate_id"]),
            )
    return DispatchResult(event_key, "dispatched", rq_job_id, False)


def dispatch_pending(limit: int = 50) -> list[DispatchResult]:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE outbox_events
            SET status = 'pending', locked_at = NULL, updated_at = NOW(),
                error_type = 'StaleDispatch',
                error_message = 'processing lease expired before dispatch confirmation'
            WHERE status = 'processing' AND locked_at < NOW() - INTERVAL '5 minutes'
            """
        )
        cur.execute(
            """
            SELECT event_key FROM outbox_events
            WHERE status IN ('pending', 'failed')
              AND attempts < max_attempts AND available_at <= NOW()
            ORDER BY id LIMIT %s
            """,
            (limit,),
        )
        keys = [row["event_key"] for row in cur.fetchall()]
    return [dispatch_event(key) for key in keys]


def requeue_failed_agent_run(agent_run_id: int) -> str:
    event_key = f"agent-run:{agent_run_id}:retry:{uuid4()}"
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_runs
            SET status = 'pending', rq_job_id = NULL, finished_at = NULL,
                error_type = NULL, error_message = NULL
            WHERE id = %s AND status = 'failed'
            RETURNING id
            """,
            (agent_run_id,),
        )
        if cur.fetchone() is None:
            raise ValueError("Agent Run 不存在或当前状态不可重新入队")
        cur.execute(
            """
            INSERT INTO outbox_events (event_key, event_type, aggregate_id, payload)
            VALUES (%s, 'agent_run', %s, jsonb_build_object('agent_run_id', %s))
            """,
            (event_key, agent_run_id, agent_run_id),
        )
    return event_key


def requeue_failed_command(command_id: int) -> str:
    with connect(row_factory=dict_row) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE github_commands gc
            SET status = 'approved', error_message = NULL, updated_at = NOW()
            WHERE gc.id = %s AND gc.status = 'failed' AND gc.retry_safe
              AND (
                    -- human 授权：必须有已 approved 的 review_task
                    (gc.authorization_source = 'human'
                     AND EXISTS (
                         SELECT 1 FROM review_tasks rt
                         WHERE rt.id = gc.review_task_id
                           AND rt.status = 'approved'
                     ))
                    -- policy 授权：必须携带 policy_version，无 review_task
                    OR (gc.authorization_source = 'policy'
                        AND gc.review_task_id IS NULL
                        AND gc.policy_version IS NOT NULL
                        AND gc.agent_run_id IS NOT NULL)
              )
            RETURNING gc.authorization_source, gc.agent_run_id, gc.review_task_id
            """,
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("GitHub Command 不存在或当前状态不可重新入队")
        source = row["authorization_source"]
        agent_run_id = row["agent_run_id"]
        if source == "policy":
            event_key = f"github-commands:{agent_run_id}:retry:{uuid4()}"
            cur.execute(
                """
                INSERT INTO outbox_events (event_key, event_type, aggregate_id, payload)
                VALUES (%s, 'github_commands', %s,
                        jsonb_build_object('agent_run_id', %s, 'command_id', %s))
                """,
                (event_key, agent_run_id, agent_run_id, command_id),
            )
        else:
            review_task_id = row["review_task_id"]
            event_key = f"review-commands:{review_task_id}:retry:{uuid4()}"
            cur.execute(
                """
                INSERT INTO outbox_events (event_key, event_type, aggregate_id, payload)
                VALUES (%s, 'review_commands', %s,
                        jsonb_build_object('review_task_id', %s, 'command_id', %s))
                """,
                (event_key, review_task_id, review_task_id, command_id),
            )
    return event_key
