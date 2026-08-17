"""确定性 Policy Gate。

职责：把 Agent 结果转成 AutomationDecision。
- 是确定性代码，不是“再问一次 LLM”。
- 决策顺序必须 fail-closed：高风险、检索降级、疑似重复、
  无动作、缺冻结策略、动作不被策略允许、置信度不足、证据不足。
- 阈值来自 calibration artifact，不写死在代码里。
- raw LLM confidence 只能作为 signal，不能直接当成 production 可信度。
"""

from app.automation.models import (
    ActionIntent,
    AutomationAction,
    AutomationDecision,
    AutomationDisposition,
    DeferReasonCode,
    HumanHandoff,
)
from app.automation.policy_loader import CalibratedPolicy

MODE_VALUES = ("off", "shadow", "enforce")


def _security_handoff(evidence: list[str]) -> HumanHandoff:
    return HumanHandoff(
        reason_code=DeferReasonCode.SECURITY_RISK,
        reason=(
            "该 Issue 涉及安全/隐私/认证等高风险内容，"
            "策略禁止自动公开处理。"
        ),
        human_task="判断该 Issue 是否需要转入私密安全响应流程。",
        evidence=evidence,
        already_checked=["已完成基础分类", "已完成风险等级识别"],
    )


def _retrieval_degraded_handoff() -> HumanHandoff:
    return HumanHandoff(
        reason_code=DeferReasonCode.RETRIEVAL_DEGRADED,
        reason=(
            "历史 Issue 检索发生降级，系统无法可靠排除重复问题。"
        ),
        human_task="确认当前 Issue 是否已有历史重复 Issue。",
        evidence=[],
        already_checked=[
            "已完成历史 Issue 检索（发生降级）",
            "已完成基础分类",
        ],
    )


def decide_automation(
    result,
    *,
    mode: str,
    calibrated_policy: CalibratedPolicy | None,
) -> AutomationDecision:
    """把 Agent 结果转成确定性 AutomationDecision。

    result 是鸭子类型，至少提供：
      risk_level: str
      retrieval_degraded: bool
      duplicate_assessment: dict | None
      proposed_actions: list[AutomationAction]
      repo, issue_number: str, int
    """
    if mode not in MODE_VALUES:
        raise ValueError(f"未知 automation mode: {mode!r}")

    shadow = mode in {"off", "shadow"}

    # 1. 安全风险永远 fail closed。
    if result.risk_level == "high":
        return AutomationDecision(
            disposition=AutomationDisposition.DEFER,
            policy_version=_policy_version(calibrated_policy),
            handoff=_security_handoff(
                [f"风险等级：{result.risk_level}"]
            ),
            shadow=shadow,
        )

    # 2. 检索降级 -> 无法可靠排除重复。
    if result.retrieval_degraded:
        return AutomationDecision(
            disposition=AutomationDisposition.DEFER,
            policy_version=_policy_version(calibrated_policy),
            handoff=_retrieval_degraded_handoff(),
            shadow=shadow,
        )

    # 3. 疑似重复：当前 Retriever 离线覆盖率不足，绝不自动关闭。
    duplicate = result.duplicate_assessment or {}
    if duplicate.get("is_duplicate"):
        candidate = duplicate.get("candidate_issue_number")
        return AutomationDecision(
            disposition=AutomationDisposition.DEFER,
            policy_version=_policy_version(calibrated_policy),
            handoff=_duplicate_handoff(result, candidate),
            shadow=shadow,
        )

    # 4. 没有待执行动作 -> NO_ACTION。
    actions = [
        action
        for action in result.proposed_actions
        if isinstance(action, AutomationAction)
    ]
    if not actions:
        return AutomationDecision(
            disposition=AutomationDisposition.NO_ACTION,
            policy_version=_policy_version(calibrated_policy),
            shadow=shadow,
        )

    # 5. enforce 模式缺少冻结策略 -> fail closed。
    if mode == "enforce" and calibrated_policy is None:
        return AutomationDecision(
            disposition=AutomationDisposition.DEFER,
            policy_version="unknown",
            handoff=HumanHandoff(
                reason_code=DeferReasonCode.POLICY_BLOCKED,
                reason=(
                    f"当前 automation mode 为 {mode}，但缺少经过离线评测冻结的"
                    "calibrated policy artifact，系统无法确认任何动作可安全自动执行。"
                ),
                human_task="先配置并冻结 automation policy artifact，再启用 enforce 模式。",
                evidence=[],
                already_checked=["已检查冻结策略存在性"],
            ),
            shadow=shadow,
        )

    # 6. 逐个动作检查策略规则，fail closed。
    #    AUTO_EXECUTE 必须同时满足：rule 存在、enabled=True、allow_auto=True
    #    （即 is_auto_enabled）、confidence 阈值、evidence 要求。
    for action in actions:
        # P1.5：REQUEST_MISSING_INFORMATION 必须拥有独立校准才能自动执行；
        # 不能因为 add_category_label 通过 calibration 就放行缺失信息回复。
        # 该 intent 没有独立校准数据时永远 DEFER。
        if action.intent == ActionIntent.REQUEST_MISSING_INFORMATION:
            rule = (
                calibrated_policy.rule_for(action.intent)
                if calibrated_policy is not None
                else None
            )
            independently_calibrated = (
                rule is not None
                and rule.enabled
                and rule.allow_auto
                and rule.observed_precision is not None
                and rule.sample_count > 0
            )
            if not independently_calibrated:
                return AutomationDecision(
                    disposition=AutomationDisposition.DEFER,
                    policy_version=_policy_version(calibrated_policy),
                    handoff=_policy_blocked_handoff(result, action),
                    shadow=shadow,
                )
        rule = (
            calibrated_policy.rule_for(action.intent)
            if calibrated_policy is not None
            else None
        )
        auto_enabled = (
            calibrated_policy.is_auto_enabled(action.intent)
            if calibrated_policy is not None
            else False
        )
        if rule is None or not auto_enabled:
            return AutomationDecision(
                disposition=AutomationDisposition.DEFER,
                policy_version=_policy_version(calibrated_policy),
                handoff=_policy_blocked_handoff(result, action),
                shadow=shadow,
            )
        if action.confidence < rule.min_model_confidence:
            return AutomationDecision(
                disposition=AutomationDisposition.DEFER,
                policy_version=_policy_version(calibrated_policy),
                handoff=_low_confidence_handoff(result, action, rule.min_model_confidence),
                shadow=shadow,
            )
        if rule.require_evidence and not action.evidence:
            return AutomationDecision(
                disposition=AutomationDisposition.DEFER,
                policy_version=_policy_version(calibrated_policy),
                handoff=_insufficient_evidence_handoff(result, action),
                shadow=shadow,
            )

    return AutomationDecision(
        disposition=AutomationDisposition.AUTO_EXECUTE,
        policy_version=_policy_version(calibrated_policy),
        actions=actions,
        shadow=shadow,
    )


def _policy_version(policy: CalibratedPolicy | None) -> str:
    return policy.policy_version if policy is not None else "no-policy"


def _duplicate_handoff(result, candidate) -> HumanHandoff:
    evidence = list(result.duplicate_assessment.get("evidence", []))
    rationale = result.duplicate_assessment.get("rationale", "")
    return HumanHandoff(
        reason_code=DeferReasonCode.DUPLICATE_UNCERTAIN,
        reason=(
            f"系统检索到疑似重复候选 #{candidate}，但当前重复检索尚未达到"
            "自动执行所需的可靠性，需要确认是否与当前 Issue 属于同一核心问题。"
        ),
        human_task=(
            f"只需确认当前 Issue #{result.issue_number} 与 #{candidate} "
            "是否属于同一根因。"
        ),
        evidence=evidence if evidence else [rationale],
        already_checked=[
            "已完成历史 Issue 检索",
            "已完成候选语义比较",
            "已完成基础分类",
        ],
    )


def _policy_blocked_handoff(result, action: AutomationAction) -> HumanHandoff:
    return HumanHandoff(
        reason_code=DeferReasonCode.POLICY_BLOCKED,
        reason=(
            f"动作 {action.type}={action.value}（intent={action.intent.value}）"
            "未被冻结策略允许自动执行，策略禁止自动公开处理。"
        ),
        human_task=f"判断当前 Issue #{result.issue_number} 是否需要人工执行该动作。",
        evidence=[
            f"intent: {action.intent.value}",
            f"action: {action.type}={action.value}",
        ],
        already_checked=["已完成动作意图识别", "已完成冻结策略匹配"],
    )


def _low_confidence_handoff(
    result, action: AutomationAction, threshold: float
) -> HumanHandoff:
    return HumanHandoff(
        reason_code=DeferReasonCode.LOW_CALIBRATED_CONFIDENCE,
        reason=(
            f"动作 {action.type}={action.value} 的模型置信度"
            f"（{action.confidence:.3f}）低于冻结策略阈值 {threshold:.3f}，"
            "策略禁止自动执行。"
        ),
        human_task=(
            f"判断当前 Issue #{result.issue_number} 的动作"
            f"“{action.type}={action.value}”是否仍应人工执行。"
        ),
        evidence=[
            f"action: {action.type}={action.value}",
            f"confidence: {action.confidence:.3f}",
            f"threshold: {threshold:.3f}",
        ],
        already_checked=["已完成动作意图识别", "已完成置信度与阈值比较"],
    )


def _insufficient_evidence_handoff(result, action: AutomationAction) -> HumanHandoff:
    return HumanHandoff(
        reason_code=DeferReasonCode.INSUFFICIENT_EVIDENCE,
        reason=(
            f"动作 {action.type}={action.value} 缺少可供校验的证据，"
            "策略禁止自动执行。"
        ),
        human_task=f"判断当前 Issue #{result.issue_number} 是否应人工执行该动作。",
        evidence=[f"action: {action.type}={action.value}"],
        already_checked=["已完成动作证据完整性检查"],
    )


def action_intent_from_type(type_value: str) -> ActionIntent:
    """把旧式 ProposedAction.type 映射为意图枚举（兼容旧 Agent 输出）。"""
    mapping = {
        "add_label": ActionIntent.ADD_CATEGORY_LABEL,
        "post_comment": ActionIntent.POST_TECHNICAL_REPLY,
    }
    return mapping.get(type_value, ActionIntent.POST_TECHNICAL_REPLY)
