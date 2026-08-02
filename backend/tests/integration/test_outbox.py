import os
from uuid import uuid4

import pytest
from psycopg.rows import dict_row

from app.db.connection import connect
from app.services.outbox import dispatch_event
from app.workers import queue

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="requires a migrated PostgreSQL database"
)


def _insert_event(event_key: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbox_events (event_key, event_type, aggregate_id, payload)
            VALUES (%s, 'review_commands', 999999,
                    jsonb_build_object('review_task_id', 999999))
            """,
            (event_key,),
        )


def _get_event(event_key: str) -> dict:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, attempts, rq_job_id, error_type, error_message
            FROM outbox_events WHERE event_key = %s
            """,
            (event_key,),
        )
        return dict(cur.fetchone())


def _delete_event(event_key: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM outbox_events WHERE event_key = %s", (event_key,))


def test_dispatch_failure_remains_recoverable(monkeypatch):
    event_key = f"integration-failure:{uuid4()}"
    _insert_event(event_key)
    monkeypatch.setattr(
        queue,
        "enqueue_review_commands",
        lambda *_: (_ for _ in ()).throw(ConnectionError("redis test failure")),
    )
    try:
        result = dispatch_event(event_key)
        event = _get_event(event_key)
        assert result.recovery_pending is True
        assert event["status"] == "pending"
        assert event["attempts"] == 1
        assert event["error_type"] == "ConnectionError"
        assert "redis test failure" in event["error_message"]
    finally:
        _delete_event(event_key)

def test_dispatch_success_records_rq_job(monkeypatch):
    event_key = f"integration-success:{uuid4()}"
    _insert_event(event_key)
    monkeypatch.setattr(queue, "enqueue_review_commands", lambda *_: "rq-test-job")
    try:
        result = dispatch_event(event_key)
        event = _get_event(event_key)
        assert result.recovery_pending is False
        assert event["status"] == "dispatched"
        assert event["rq_job_id"] == "rq-test-job"
    finally:
        _delete_event(event_key)
