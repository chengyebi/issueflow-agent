"""IssueFlow V2 选择性自动化：领域模型、确定性 Policy Gate 与人工接管。"""

from app.automation.models import (
    ActionIntent,
    AutomationAction,
    AutomationDecision,
    AutomationDisposition,
    DeferReasonCode,
    HumanHandoff,
)
from app.automation.policy import decide_automation
from app.automation.policy_loader import (
    CalibratedPolicy,
    PolicyLoaderError,
    load_calibrated_policy,
)
from app.automation.handoff import render_missing_information_comment

__all__ = [
    "ActionIntent",
    "AutomationAction",
    "AutomationDecision",
    "AutomationDisposition",
    "DeferReasonCode",
    "HumanHandoff",
    "CalibratedPolicy",
    "PolicyLoaderError",
    "load_calibrated_policy",
    "decide_automation",
    "render_missing_information_comment",
]
