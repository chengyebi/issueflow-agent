"""选择性自动化领域模型。

这些模型描述 Policy Gate 的输出语义：

- AutomationDecision 是 Agent 结果经过确定性 Policy Gate 之后的最终裁定。
- 不允许模糊状态：AUTO_EXECUTE 必须没有 handoff，DEFER 必须有完整 handoff，
  NO_ACTION 必须没有 actions。
- confidence 只是模型/上游信号，绝不能直接当作真实概率；
  production AUTO_EXECUTE 由经过离线评测冻结的 policy artifact 约束。
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AutomationDisposition(StrEnum):
    AUTO_EXECUTE = "auto_execute"
    DEFER = "defer"
    NO_ACTION = "no_action"


class DeferReasonCode(StrEnum):
    SECURITY_RISK = "security_risk"
    RETRIEVAL_DEGRADED = "retrieval_degraded"
    DUPLICATE_UNCERTAIN = "duplicate_uncertain"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    LOW_CALIBRATED_CONFIDENCE = "low_calibrated_confidence"
    UNSUPPORTED_ACTION = "unsupported_action"
    POLICY_BLOCKED = "policy_blocked"
    OUT_OF_DISTRIBUTION = "out_of_distribution"
    MODEL_FAILURE = "model_failure"


class ActionIntent(StrEnum):
    ADD_CATEGORY_LABEL = "add_category_label"
    REQUEST_MISSING_INFORMATION = "request_missing_information"
    POST_TECHNICAL_REPLY = "post_technical_reply"
    DUPLICATE_ACTION = "duplicate_action"


class AutomationAction(BaseModel):
    type: Literal["add_label", "post_comment"]
    value: str
    intent: ActionIntent

    # 模型或上游节点给出的信号，不可直接当成真实概率。
    confidence: float = Field(ge=0.0, le=1.0)

    rationale: str
    evidence: list[str] = Field(default_factory=list)


class HumanHandoff(BaseModel):
    reason_code: DeferReasonCode

    # 必须针对当前 Issue，不能写“AI 不确定，请人工检查”这种废话。
    reason: str

    # 必须是一个最小、可执行的人工任务。
    human_task: str

    evidence: list[str] = Field(default_factory=list)
    already_checked: list[str] = Field(default_factory=list)

    _GENERIC_GARBAGE_PHRASES = (
        "AI 不确定",
        "请人工审核",
        "请审核此 Issue",
        "置信度不够",
        "需要人工判断",
        "无法自动处理",
    )
    _ISSUE_SPECIFIC_MARKERS = (
        "#",  # Issue 编号，如 #1832
        "候选",
        "复现",
        "日志",
        "版本",
        "根因",
        "环境",
        "字段",
        "证据",
        "仓库",
        "策略",
        "安全",
        "风险等级",
        "动作",
    )

    @model_validator(mode="after")
    def require_specific_context(self):
        if not self.reason.strip():
            raise ValueError("handoff.reason 不能为空")
        if not self.human_task.strip():
            raise ValueError("handoff.human_task 不能为空")
        # 禁止“AI 不确定，请人工审核”这类没有任何价值的泛化模板。
        # 规则：reason/human_task 中出现泛化废话短语时，整个 handoff 必须
        # 至少包含一条针对当前 Issue 或策略的具体上下文，否则拒绝。
        # 纯策略性 reason（如“策略禁止自动公开处理”）不属于废话，仍可放行。
        if any(phrase in self.reason for phrase in self._GENERIC_GARBAGE_PHRASES):
            joined = (
                self.reason + "\n" + self.human_task + "\n"
                + "\n".join(self.evidence + self.already_checked)
            )
            if not any(
                marker in joined for marker in self._ISSUE_SPECIFIC_MARKERS
            ):
                raise ValueError(
                    "handoff 不能只有“AI 不确定，请人工审核”这类泛化模板，"
                    "必须包含针对当前 Issue 的具体上下文"
                )
        return self


class AutomationDecision(BaseModel):
    disposition: AutomationDisposition
    policy_version: str

    actions: list[AutomationAction] = Field(default_factory=list)

    handoff: HumanHandoff | None = None

    # shadow 模式下记录“本来会怎么做”。
    shadow: bool = False

    @model_validator(mode="after")
    def validate_state_is_unambiguous(self):
        if self.disposition == AutomationDisposition.AUTO_EXECUTE:
            if self.handoff is not None:
                raise ValueError("AUTO_EXECUTE 不允许携带 handoff")
            if not self.actions:
                raise ValueError("AUTO_EXECUTE 至少需要一条动作")
        if self.disposition == AutomationDisposition.DEFER:
            if self.handoff is None:
                raise ValueError("DEFER 必须携带 handoff")
            if not self.handoff.reason.strip():
                raise ValueError("DEFER.handoff.reason 不能为空")
            if not self.handoff.human_task.strip():
                raise ValueError("DEFER.handoff.human_task 不能为空")
            if self.actions:
                raise ValueError("DEFER 不允许同时携带待执行动作")
        if self.disposition == AutomationDisposition.NO_ACTION:
            if self.actions:
                raise ValueError("NO_ACTION 不允许携带动作")
            if self.handoff is not None:
                raise ValueError("NO_ACTION 不允许携带 handoff")
        return self
