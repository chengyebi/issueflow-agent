"""自动化指标聚合的隔离数据库集成测试。"""

import os

import pytest

from app.db.connection import connect
from app.services.automation_metrics import aggregate_automation_metrics

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="requires a migrated PostgreSQL database"
)


def _insert_decision(disposition: str, reason_code: str | None = None) -> int:
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO issue_events (source, event_type, repo, action, issue_number,
                                      issue_title, issue_body)
            VALUES ('github', 'issue', 'owner/repo', 'opened', 1, 't', 'b')
            RETURNING id
            """,
        )
        issue_event_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO agent_runs (issue_event_id, status)
            VALUES (%s, 'completed')
            RETURNING id
            """,
            (issue_event_id,),
        )
        agent_run_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO automation_decisions
                (agent_run_id, disposition, policy_version, shadow, reason_code)
            VALUES (%s, %s, 'v1', FALSE, %s)
            """,
            (agent_run_id, disposition, reason_code),
        )
        return agent_run_id


def _cleanup(agent_run_id: int) -> None:
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM automation_decisions WHERE agent_run_id = %s", (agent_run_id,))
        cur.execute("SELECT issue_event_id FROM agent_runs WHERE id = %s", (agent_run_id,))
        row = cur.fetchone()
        cur.execute("DELETE FROM agent_runs WHERE id = %s", (agent_run_id,))
        if row is not None:
            cur.execute("DELETE FROM issue_events WHERE id = %s", (row[0],))


def test_aggregate_automation_metrics(monkeypatch):
    run_ids = []
    try:
        run_ids.append(_insert_decision("auto_execute"))
        run_ids.append(_insert_decision("auto_execute"))
        run_ids.append(_insert_decision("defer", "duplicate_uncertain"))
        run_ids.append(_insert_decision("defer", "security_risk"))
        run_ids.append(_insert_decision("no_action"))
    finally:
        metrics = aggregate_automation_metrics()
        assert metrics["sample_count"] >= 5
        assert metrics["auto_execute_rate"] > 0
        assert metrics["defer_rate"] > 0
        assert metrics["no_action_rate"] > 0
        assert metrics["human_touch_rate"] >= metrics["defer_rate"]
        assert metrics["defer_reason_distribution"].get("duplicate_uncertain", 0) >= 1
        for run_id in run_ids:
            _cleanup(run_id)
