"""Agent Run 完成后的选择性自动化路由。

核心职责：在一个可靠事务中
  1. 保存 agent_runs completed/result/trace/cost；
  2. 计算 AutomationDecision（确定性 Policy Gate）；
  3. 保存 automation_decisions；
  4. 根据 mode + disposition 路由：
     - ENFORCE + AUTO_EXECUTE -> policy 授权的 github_commands + Outbox
     - DEFER               -> 创建 review_task + human proposed commands + handoff
     - NO_ACTION           -> 不创建 review_task、不创建命令
  5. SHADOW / OFF 模式：真实 side effect 仍走人工路径，但记录 policy 裁定。

要求：事务一致性、idempotency、Outbox、恢复语义。
"""

from dataclasses import dataclass, field
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.automation.models import (
    AutomationAction,
    AutomationDecision,
    AutomationDisposition,
)
from app.automation.policy import decide_automation
from app.automation.policy_loader import CalibratedPolicy, load_calibrated_policy
from app.core.config import get_settings
from app.db.connection import connect


@dataclass(frozen=True)
class RouteOutcome:
    disposition: str
    review_task_id: int | None = None
    command_ids: list[int] = field(default_factory=list)
    outbox_event_key: str | None = None
    shadow: bool = False


class _ResultView:
    """把 result dict 包装成 decide_automation 需要的鸭子类型。"""

    def __init__(self, result: dict):
        self._result = result
        self.risk_level = result.get("risk_level", "low")
        self.retrieval_degraded = bool(result.get("retrieval_degraded", False))
        self.duplicate_assessment = result.get("duplicate_assessment") or {}
        self.repo = result.get("repo", "")
        self.issue_number = result.get("issue_number", 0)
        self.proposed_actions = [
            AutomationAction.model_validate(action)
            for action in result.get("proposed_actions", [])
            if isinstance(action, dict)
        ]


def load_policy_for_mode(mode: str) -> CalibratedPolicy | None:
    """按 mode 加载冻结策略。

    enforce 模式要求存在已冻结策略，否则返回 None（调用方 fail closed）。
    off / shadow 模式返回策略（用于记录裁定），允许为空。
    """
    if mode == "off":
        return None
    settings = get_settings()
    path = settings.automation_policy_path
    policy_path = Path(path) if path else None
    try:
        return load_calibrated_policy(policy_path)
    except Exception:
        # 加载失败统一按“无策略”处理，由 Policy Gate 决定是否 fail closed。
        return None


def compute_decision(result: dict, mode: str) -> AutomationDecision:
    policy = load_policy_for_mode(mode)
    return decide_automation(
        _ResultView(result),
        mode=mode,
        calibrated_policy=policy,
    )


def save_completed_run_and_route(
    agent_run_id: int,
    result: dict,
    duration_ms: int,
    trace,
    estimated_cost_usd: float | None,
) -> RouteOutcome:
    """保存完成的 run 并按 automation mode / disposition 路由。"""
    settings = get_settings()
    mode = settings.automation_mode
    decision = compute_decision(result, mode)

    with connect(row_factory=dict_row) as conn, conn.transaction(), conn.cursor() as cur:
        _save_run(cur, agent_run_id, result, duration_ms, trace, estimated_cost_usd)
        _save_duplicate_assessment(cur, agent_run_id, result)
        _save_automation_decision(cur, agent_run_id, decision)

        outcome = _route(
            cur,
            agent_run_id,
            result,
            decision,
            mode,
        )

    return outcome


def _save_run(cur, agent_run_id: int, result: dict, duration_ms: int, trace, estimated_cost_usd: float | None) -> None:
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


def _save_duplicate_assessment(cur, agent_run_id: int, result: dict) -> None:
    duplicate = result.get("duplicate_assessment") or {}
    if "is_duplicate" not in duplicate:
        return
    cur.execute(
        """
        INSERT INTO duplicate_assessments (
            agent_run_id, repo, issue_number, is_duplicate,
            candidate_issue_number, confidence, rationale,
            evidence, retrieval_mode
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (agent_run_id) DO UPDATE SET
            is_duplicate = EXCLUDED.is_duplicate,
            candidate_issue_number = EXCLUDED.candidate_issue_number,
            confidence = EXCLUDED.confidence,
            rationale = EXCLUDED.rationale,
            evidence = EXCLUDED.evidence,
            retrieval_mode = EXCLUDED.retrieval_mode
        """,
        (
            agent_run_id,
            result["repo"],
            result["issue_number"],
            duplicate["is_duplicate"],
            duplicate.get("candidate_issue_number"),
            duplicate.get("confidence", 0.0),
            duplicate.get("rationale", ""),
            Jsonb(duplicate.get("evidence", [])),
            result.get("retrieval_mode", "lexical"),
        ),
    )


def _save_automation_decision(cur, agent_run_id: int, decision: AutomationDecision) -> None:
    handoff = decision.handoff
    cur.execute(
        """
        INSERT INTO automation_decisions (
            agent_run_id, disposition, policy_version, shadow,
            reason_code, reason, human_task, evidence, already_checked
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (agent_run_id) DO UPDATE SET
            disposition = EXCLUDED.disposition,
            policy_version = EXCLUDED.policy_version,
            shadow = EXCLUDED.shadow,
            reason_code = EXCLUDED.reason_code,
            reason = EXCLUDED.reason,
            human_task = EXCLUDED.human_task,
            evidence = EXCLUDED.evidence,
            already_checked = EXCLUDED.already_checked
        """,
        (
            agent_run_id,
            decision.disposition.value,
            decision.policy_version,
            decision.shadow,
            handoff.reason_code.value if handoff else None,
            handoff.reason if handoff else None,
            handoff.human_task if handoff else None,
            Jsonb(handoff.evidence if handoff else []),
            Jsonb(handoff.already_checked if handoff else []),
        ),
    )


def _route(cur, agent_run_id: int, result: dict, decision: AutomationDecision, mode: str) -> RouteOutcome:
    disposition = decision.disposition
    shadow = decision.shadow

    if mode == "off":
        # 紧急回退：完全兼容旧 review-all 行为，始终走人工审核。
        return _route_human_review(cur, agent_run_id, result)

    if mode == "shadow":
        # shadow：记录裁定，但真实动作仍走人工（除非裁定为 NO_ACTION）。
        if disposition == AutomationDisposition.NO_ACTION:
            return RouteOutcome(
                disposition=disposition.value, shadow=True
            )
        return _route_human_review(cur, agent_run_id, result)

    # mode == enforce
    if disposition == AutomationDisposition.AUTO_EXECUTE:
        return _route_auto_execute(cur, agent_run_id, result, decision)
    if disposition == AutomationDisposition.DEFER:
        return _route_human_review(cur, agent_run_id, result)
    return RouteOutcome(disposition=disposition.value)


def _create_review_task(cur, agent_run_id: int) -> int:
    cur.execute(
        """
        INSERT INTO review_tasks (agent_run_id)
        VALUES (%s)
        ON CONFLICT (agent_run_id)
        DO UPDATE SET agent_run_id = EXCLUDED.agent_run_id
        RETURNING id;
        """,
        (agent_run_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("创建审核任务后没有返回 ID")
    return row["id"] if isinstance(row, dict) else row[0]


def _route_human_review(cur, agent_run_id: int, result: dict) -> RouteOutcome:
    review_task_id = _create_review_task(cur, agent_run_id)
    command_ids: list[int] = []
    actions = [
        AutomationAction.model_validate(a)
        for a in result.get("proposed_actions", [])
        if isinstance(a, dict)
    ]
    for index, action in enumerate(actions):
        command_id = _insert_human_command(cur, agent_run_id, review_task_id, index, action)
        command_ids.append(command_id)
    return RouteOutcome(
        disposition="defer",
        review_task_id=review_task_id,
        command_ids=command_ids,
        shadow=True,
    )


def _route_auto_execute(cur, agent_run_id: int, result: dict, decision: AutomationDecision) -> RouteOutcome:
    command_ids: list[int] = []
    for index, action in enumerate(decision.actions):
        command_id = _insert_policy_command(cur, agent_run_id, index, action, decision)
        command_ids.append(command_id)

    outbox_event_key = None
    if command_ids:
        outbox_event_key = f"github-commands:{agent_run_id}"
        cur.execute(
            """
            INSERT INTO outbox_events (
                event_key, event_type, aggregate_id, payload
            ) VALUES (
                %s, 'github_commands', %s,
                jsonb_build_object('agent_run_id', %s, 'command_ids', %s)
            )
            ON CONFLICT (event_key) DO NOTHING
            """,
            (outbox_event_key, agent_run_id, agent_run_id, Jsonb(command_ids)),
        )
    return RouteOutcome(
        disposition="auto_execute",
        command_ids=command_ids,
        outbox_event_key=outbox_event_key,
        shadow=False,
    )


def _insert_policy_command(cur, agent_run_id: int, index: int, action: AutomationAction, decision: AutomationDecision) -> int:
    idempotency_key = f"agent-run:{agent_run_id}:action:{index}:{action.type}"
    cur.execute(
        """
        INSERT INTO github_commands (
            agent_run_id, review_task_id, command_type, payload,
            idempotency_key, status, authorization_source,
            authorization_reason, policy_version, action_intent,
            action_confidence, rationale, evidence
        )
        VALUES (%s, NULL, %s, %s, %s, 'approved', 'policy',
                %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id;
        """,
        (
            agent_run_id,
            action.type,
            Jsonb({"value": action.value}),
            idempotency_key,
            "策略授权自动执行",
            decision.policy_version,
            action.intent.value,
            action.confidence,
            action.rationale,
            Jsonb(action.evidence),
        ),
    )
    row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


def _insert_human_command(cur, agent_run_id: int, review_task_id: int, index: int, action: AutomationAction) -> int:
    idempotency_key = f"agent-run:{agent_run_id}:action:{index}:{action.type}"
    cur.execute(
        """
        INSERT INTO github_commands (
            agent_run_id, review_task_id, command_type, payload,
            idempotency_key, status, authorization_source,
            action_intent, action_confidence, rationale, evidence
        )
        VALUES (%s, %s, %s, %s, %s, 'proposed', 'human',
                %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id;
        """,
        (
            agent_run_id,
            review_task_id,
            action.type,
            Jsonb({"value": action.value}),
            idempotency_key,
            action.intent.value,
            action.confidence,
            action.rationale,
            Jsonb(action.evidence),
        ),
    )
    row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]
