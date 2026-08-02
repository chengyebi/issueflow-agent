from dataclasses import dataclass

from psycopg.rows import dict_row

from app.core.config import get_settings
from app.db.connection import connect
from app.models.issues import InternalIssueEvent


@dataclass(frozen=True)
class AcceptedIssueDelivery:
    is_new: bool
    issue_event_id: int | None = None
    agent_run_id: int | None = None
    outbox_event_key: str | None = None


def save_issue_event(event: InternalIssueEvent) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO issue_events (
                source, event_type, repo, action, issue_number,
                issue_title, issue_body
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                event.source,
                event.event_type,
                event.repo,
                event.action,
                event.issue_number,
                event.issue_title,
                event.issue_body,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("插入 Issue 事件后没有返回 ID")
        return row[0]


def save_webhook_delivery(
    delivery_id: str,
    event_name: str,
    payload_body: bytes,
) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_deliveries (delivery_id, event_name, raw_payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING id
            """,
            (delivery_id, event_name, payload_body.decode("utf-8")),
        )
        return cur.fetchone() is not None


def accept_issue_delivery(
    delivery_id: str,
    event_name: str,
    payload_body: bytes,
    event: InternalIssueEvent,
) -> AcceptedIssueDelivery:
    """Persist delivery, normalized event and run in one transaction."""
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_deliveries (delivery_id, event_name, raw_payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING id
            """,
            (delivery_id, event_name, payload_body.decode("utf-8")),
        )
        delivery = cur.fetchone()
        if delivery is None:
            return AcceptedIssueDelivery(is_new=False)

        cur.execute(
            """
            INSERT INTO issue_events (
                source, event_type, repo, action, issue_number,
                issue_title, issue_body, webhook_delivery_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                event.source,
                event.event_type,
                event.repo,
                event.action,
                event.issue_number,
                event.issue_title,
                event.issue_body,
                delivery[0],
            ),
        )
        issue = cur.fetchone()
        if issue is None:
            raise RuntimeError("插入 Issue 事件后没有返回 ID")

        cur.execute(
            "INSERT INTO agent_runs (issue_event_id) VALUES (%s) RETURNING id",
            (issue[0],),
        )
        run = cur.fetchone()
        if run is None:
            raise RuntimeError("创建 Agent Run 后没有返回 ID")
        event_key = f"agent-run:{run[0]}"
        cur.execute(
            """
            INSERT INTO outbox_events (
                event_key, event_type, aggregate_id, payload, max_attempts
            ) VALUES (%s, 'agent_run', %s, jsonb_build_object('agent_run_id', %s), %s)
            ON CONFLICT (event_key) DO NOTHING
            """,
            (event_key, run[0], run[0], get_settings().outbox_max_attempts),
        )
        return AcceptedIssueDelivery(True, issue[0], run[0], event_key)


def save_agent_run_job_id(agent_run_id: int, rq_job_id: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runs SET rq_job_id = %s WHERE id = %s",
            (rq_job_id, agent_run_id),
        )


def mark_agent_run_failed(agent_run_id: int, error_message: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_runs
            SET status = 'failed', finished_at = NOW(), error_message = %s
            WHERE id = %s
            """,
            (error_message[:2000], agent_run_id),
        )


def list_issue_events(limit: int = 20) -> list[dict]:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source, event_type, repo, action, issue_number,
                   issue_title, issue_body, created_at
            FROM issue_events ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())
