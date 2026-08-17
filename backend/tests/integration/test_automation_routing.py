"""选择性自动化路由的隔离数据库集成测试。

覆盖：
  8.  shadow mode 不真实自动写回
  9.  enforce auto path 不创建 review_task
  10. defer path 必须创建 review_task
  11. auto github_command：review_task_id IS NULL + authorization_source == policy
  12. human github_command：review_task_id != NULL + authorization_source == human
  13. worker 拒绝无合法 authorization 的 command
  17. idempotency 不退化
  18. retry/recovery 不退化
"""

import os
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.config import clear_settings_cache, get_settings
from app.db.connection import connect
from app.services.automation_router import save_completed_run_and_route
from app.services.outbox import dispatch_event, requeue_failed_command

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="requires a migrated PostgreSQL database"
)


def _insert_agent_run(repo: str, issue_number: int) -> int:
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO issue_events (source, event_type, repo, action, issue_number,
                                      issue_title, issue_body)
            VALUES ('github', 'issue', %s, 'opened', %s, 't', 'b')
            RETURNING id
            """,
            (repo, issue_number),
        )
        issue_event_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO agent_runs (issue_event_id, status)
            VALUES (%s, 'running')
            RETURNING id
            """,
            (issue_event_id,),
        )
        return cur.fetchone()[0]


def _delete_agent_run(agent_run_id: int) -> None:
    with connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM agent_node_traces WHERE agent_run_id = %s", (agent_run_id,))
        cur.execute("DELETE FROM automation_decisions WHERE agent_run_id = %s", (agent_run_id,))
        cur.execute("DELETE FROM github_commands WHERE agent_run_id = %s", (agent_run_id,))
        cur.execute(
            """
            DELETE FROM github_commands gc
            USING review_tasks rt
            WHERE gc.review_task_id = rt.id AND rt.agent_run_id = %s
            """,
            (agent_run_id,),
        )
        cur.execute("DELETE FROM duplicate_assessments WHERE agent_run_id = %s", (agent_run_id,))
        cur.execute("DELETE FROM review_tasks WHERE agent_run_id = %s", (agent_run_id,))
        cur.execute("SELECT issue_event_id FROM agent_runs WHERE id = %s", (agent_run_id,))
        row = cur.fetchone()
        cur.execute("DELETE FROM agent_runs WHERE id = %s", (agent_run_id,))
        if row is not None:
            cur.execute("DELETE FROM issue_events WHERE id = %s", (row[0],))


def _label_action(confidence=0.95):
    return {
        "type": "add_label",
        "value": "bug",
        "intent": "add_category_label",
        "confidence": confidence,
        "rationale": "可复现的软件异常",
        "evidence": ["错误发生在保存时"],
    }


def _comment_action():
    return {
        "type": "post_comment",
        "value": "请补充日志",
        "intent": "request_missing_information",
        "confidence": 0.9,
        "rationale": "缺少复现信息",
        "evidence": ["缺失字段：错误日志"],
    }


def _result(**overrides):
    base = {
        "repo": "microsoft/vscode",
        "issue_number": 1,
        "category": "bug",
        "priority": "medium",
        "risk_level": "low",
        "confidence": 0.95,
        "missing_repro_fields": [],
        "summary": "s",
        "suggested_reply": "",
        "status": "WAITING_REVIEW",
        "proposed_actions": [],
        "retrieval_mode": "lexical",
        "retrieval_degraded": False,
    }
    base.update(overrides)
    return base


class _FakeTrace:
    input_tokens = 10
    output_tokens = 5
    structured_output_success = True


def _enable_policy_add_label(monkeypatch, tmp_path):
    """写一个临时策略 artifact，仅 add_category_label 允许自动执行。"""
    import json

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "policy_version": "itest-v1",
                "created_at": "2026-01-01T00:00:00Z",
                "source_dataset_hash": "hash",
                "prediction_artifact_hash": "itest-pred-hash",
                "rules": {
                    "add_category_label": {
                        "enabled": True,
                        "min_model_confidence": 0.80,
                        "require_evidence": True,
                        "observed_precision": 0.9,
                        "coverage": 0.5,
                        "sample_count": 100,
                        "allow_auto": True,
                    },
                    "request_missing_information": {
                        "enabled": False,
                        "min_model_confidence": 1.0,
                        "require_evidence": True,
                        "allow_auto": False,
                    },
                    "post_technical_reply": {
                        "enabled": False,
                        "min_model_confidence": 1.0,
                        "require_evidence": True,
                        "allow_auto": False,
                    },
                    "duplicate_action": {
                        "enabled": False,
                        "min_model_confidence": 1.0,
                        "require_evidence": True,
                        "allow_auto": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOMATION_POLICY_PATH", str(policy_file))
    clear_settings_cache()
    return policy_file


def test_enforce_auto_execute_creates_policy_command_no_review(
    monkeypatch, tmp_path
):
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12345)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        assert outcome.disposition == "auto_execute"
        assert outcome.review_task_id is None
        assert len(outcome.command_ids) == 1

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT review_task_id, authorization_source, policy_version,
                       status, action_intent, agent_run_id
                FROM github_commands WHERE id = %s
                """,
                (outcome.command_ids[0],),
            )
            cmd = cur.fetchone()
            assert cmd["review_task_id"] is None
            assert cmd["authorization_source"] == "policy"
            assert cmd["policy_version"] == "itest-v1"
            assert cmd["status"] == "approved"
            assert cmd["action_intent"] == "add_category_label"
            assert cmd["agent_run_id"] == agent_run_id

            cur.execute(
                "SELECT count(*) AS n FROM review_tasks WHERE agent_run_id = %s",
                (agent_run_id,),
            )
            assert cur.fetchone()["n"] == 0

            cur.execute(
                """
                SELECT disposition, policy_version, shadow
                FROM automation_decisions WHERE agent_run_id = %s
                """,
                (agent_run_id,),
            )
            decision = cur.fetchone()
            assert decision["disposition"] == "auto_execute"
            assert decision["shadow"] is False
    finally:
        _delete_agent_run(agent_run_id)


def test_enforce_defer_creates_review_task_and_human_command(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12346)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(
                proposed_actions=[_comment_action()],
                confidence=0.95,
            ),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        assert outcome.disposition == "defer"
        assert outcome.review_task_id is not None

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM review_tasks WHERE id = %s",
                (outcome.review_task_id,),
            )
            assert cur.fetchone()["status"] == "pending"

            cur.execute(
                """
                SELECT review_task_id, authorization_source, status
                FROM github_commands WHERE agent_run_id = %s
                """,
                (agent_run_id,),
            )
            cmd = cur.fetchone()
            assert cmd["review_task_id"] == outcome.review_task_id
            assert cmd["authorization_source"] == "human"
            assert cmd["status"] == "proposed"
    finally:
        _delete_agent_run(agent_run_id)


def test_shadow_mode_records_decision_but_routes_human(monkeypatch, tmp_path):
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "shadow")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12347)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        # shadow 下即使 policy 会 AUTO_EXECUTE，也走人工审核。
        assert outcome.review_task_id is not None

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT disposition, shadow FROM automation_decisions
                WHERE agent_run_id = %s
                """,
                (agent_run_id,),
            )
            decision = cur.fetchone()
            assert decision["disposition"] == "auto_execute"
            assert decision["shadow"] is True
    finally:
        _delete_agent_run(agent_run_id)


def test_no_action_routes_to_no_review(monkeypatch, tmp_path):
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12348)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        assert outcome.disposition == "no_action"
        assert outcome.review_task_id is None

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM review_tasks WHERE agent_run_id = %s",
                (agent_run_id,),
            )
            assert cur.fetchone()["n"] == 0
    finally:
        _delete_agent_run(agent_run_id)


def test_off_mode_completely_keeps_review_all_behavior(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "off")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12349)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        assert outcome.review_task_id is not None
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT review_task_id, authorization_source
                FROM github_commands WHERE agent_run_id = %s
                """,
                (agent_run_id,),
            )
            cmd = cur.fetchone()
            assert cmd["review_task_id"] == outcome.review_task_id
            assert cmd["authorization_source"] == "human"
    finally:
        _delete_agent_run(agent_run_id)


def test_auto_execute_uses_outbox_github_commands(monkeypatch, tmp_path):
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12350)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        assert outcome.outbox_event_key == f"github-commands:{agent_run_id}"

        monkeypatch.setattr(
            "app.workers.queue.enqueue_authorized_commands",
            lambda *_: "rq-policy-job",
        )
        result = dispatch_event(outcome.outbox_event_key)
        assert result.rq_job_id == "rq-policy-job"
        assert result.recovery_pending is False
    finally:
        _delete_agent_run(agent_run_id)


def test_requeue_failed_policy_command(monkeypatch, tmp_path):
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12351)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        command_id = outcome.command_ids[0]

        # 模拟一次可重试失败。
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE github_commands
                SET status = 'failed', retry_safe = TRUE, error_type = 'GitHubRequestError'
                WHERE id = %s
                """,
                (command_id,),
            )

        event_key = requeue_failed_command(command_id)
        assert event_key.startswith("github-commands:")
    finally:
        _delete_agent_run(agent_run_id)


def test_human_approved_command_stays_compatible(monkeypatch):
    """旧 human 路径：review approved 后命令仍可执行。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "off")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12353)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        review_task_id = outcome.review_task_id
        command_id = outcome.command_ids[0]

        # 人工批准 review -> 命令 approved，authorization_source 保持 human。
        with connect() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE review_tasks SET status = 'approved', reviewer = 'alice' WHERE id = %s",
                (review_task_id,),
            )
            cur.execute(
                "UPDATE github_commands SET status = 'approved' WHERE id = %s",
                (command_id,),
            )

        captured = {}

        def fake_add_label(repo, issue_number, label):
            captured.update({"repo": repo, "issue_number": issue_number, "label": label})
            return [{"name": label}]

        monkeypatch.setattr("app.tasks.add_issue_label", fake_add_label)

        from app.tasks import process_github_command

        result = process_github_command(command_id)
        assert result["status"] == "executed"
        assert captured["label"] == "bug"
        assert captured["repo"] == "microsoft/vscode"
        assert captured["issue_number"] == 12353
    finally:
        _delete_agent_run(agent_run_id)


def test_policy_command_executes_through_worker(monkeypatch, tmp_path):
    """policy 授权命令能通过正常 Worker 执行。"""
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12354)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        command_id = outcome.command_ids[0]

        captured = {}

        def fake_add_label(repo, issue_number, label):
            captured.update({"repo": repo, "issue_number": issue_number, "label": label})
            return [{"name": label}]

        monkeypatch.setattr("app.tasks.add_issue_label", fake_add_label)

        from app.tasks import process_github_command

        result = process_github_command(command_id)
        assert result["status"] == "executed"
        assert captured["label"] == "bug"
        assert captured["repo"] == "microsoft/vscode"
    finally:
        _delete_agent_run(agent_run_id)


def test_legacy_data_backfilled_as_human_authorized(monkeypatch):
    """模拟 migration 回填：旧式（review_task_id 非空、source NULL）命令回填为 human 授权。"""
    agent_run_id = _insert_agent_run("microsoft/vscode", 12355)
    try:
        with connect(row_factory=dict_row) as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO review_tasks (agent_run_id) VALUES (%s) RETURNING id",
                (agent_run_id,),
            )
            review_task_id = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO github_commands (
                    review_task_id, command_type, payload, idempotency_key,
                    status, authorization_source
                ) VALUES (%s, 'add_label', %s, %s, 'approved', NULL)
                RETURNING id
                """,
                (
                    review_task_id,
                    Jsonb({"value": "bug"}),
                    f"legacy:{agent_run_id}",
                ),
            )
            command_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE review_tasks SET status = 'approved' WHERE id = %s",
                (review_task_id,),
            )

            # 与 migration 0006 相同的回填逻辑。
            cur.execute(
                """
                UPDATE github_commands
                SET authorization_source = 'human'
                WHERE authorization_source IS NULL AND review_task_id IS NOT NULL
                """
            )

        from app.tasks import is_command_authorized

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status AS command_status, review_task_id,
                       authorization_source, policy_version,
                       (SELECT status FROM review_tasks WHERE id = review_task_id) AS review_status
                FROM github_commands WHERE id = %s
                """,
                (command_id,),
            )
            cmd = cur.fetchone()
            assert cmd["authorization_source"] == "human"
            assert is_command_authorized(cmd) is True
    finally:
        _delete_agent_run(agent_run_id)


def test_policy_route_idempotent_no_duplicate_side_effect(monkeypatch, tmp_path):
    """同一 agent_run 路由两次：不产生第二条 command、不重复 outbox 副作用。"""
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12360)
    try:
        first = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        second = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        # 返回相同 command id。
        assert first.command_ids == second.command_ids
        assert len(first.command_ids) == 1

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM github_commands WHERE agent_run_id = %s",
                (agent_run_id,),
            )
            assert cur.fetchone()["n"] == 1
            # outbox 事件幂等：不重复。
            cur.execute(
                """
                SELECT count(*) AS n FROM outbox_events
                WHERE event_key = %s
                """,
                (f"github-commands:{agent_run_id}",),
            )
            assert cur.fetchone()["n"] == 1
    finally:
        _delete_agent_run(agent_run_id)


def test_human_route_idempotent(monkeypatch):
    """human 路径同一 run 路由两次：不创建第二条 command。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "off")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12361)
    try:
        first = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        second = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        assert first.command_ids == second.command_ids
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM github_commands WHERE agent_run_id = %s",
                (agent_run_id,),
            )
            assert cur.fetchone()["n"] == 1
    finally:
        _delete_agent_run(agent_run_id)


def test_worker_rejects_unauthorized_command(monkeypatch, tmp_path):
    _enable_policy_add_label(monkeypatch, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "automation_mode", "enforce")

    agent_run_id = _insert_agent_run("microsoft/vscode", 12352)
    try:
        outcome = save_completed_run_and_route(
            agent_run_id,
            _result(proposed_actions=[_label_action()]),
            duration_ms=1,
            trace=_FakeTrace(),
            estimated_cost_usd=None,
        )
        command_id = outcome.command_ids[0]

        # 破坏授权：把 policy_version 清空并改成 human 授权但无 review_task。
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE github_commands
                SET authorization_source = 'human', policy_version = NULL
                WHERE id = %s
                """,
                (command_id,),
            )

        from app.tasks import is_command_authorized

        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status AS command_status, review_task_id,
                       authorization_source, policy_version,
                       NULL::text AS review_status
                FROM github_commands WHERE id = %s
                """,
                (command_id,),
            )
            cmd = cur.fetchone()
            assert is_command_authorized(cmd) is False
    finally:
        _delete_agent_run(agent_run_id)
