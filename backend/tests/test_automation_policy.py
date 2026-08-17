"""自动化 Policy Gate 与领域模型测试。

覆盖任务清单 1-8、18-21、23-24 中与模型/策略相关的部分：
  - high-risk -> DEFER
  - retrieval degraded -> DEFER
  - duplicate candidate -> DEFER
  - unsupported action -> DEFER
  - missing calibrated policy + enforce -> DEFER/fail closed
  - valid calibrated safe action -> AUTO_EXECUTE
  - no external action -> NO_ACTION
  - shadow mode 不真实自动写回（decision.shadow=True）
  - HumanHandoff validator（reason/human_task 非空、拒绝泛化模板）
  - deterministic missing-info comment
  - policy loader fail closed
"""

import pytest
from pydantic import ValidationError

from app.automation.handoff import render_missing_information_comment
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


def _policy(**rule_overrides) -> CalibratedPolicy:
    add_label_rule = {
        "enabled": True,
        "min_model_confidence": 0.80,
        "require_evidence": True,
        "allow_auto": True,
    }
    add_label_rule.update(rule_overrides.pop("add_category_label", {}))
    rules = {
        "add_category_label": add_label_rule,
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
    }
    rules.update(rule_overrides)
    return CalibratedPolicy.model_validate(
        {
            "schema_version": "1.0",
            "policy_version": "test-v1",
            "created_at": "2026-01-01T00:00:00Z",
            "source_dataset_hash": "abc",
            "rules": rules,
        }
    )


class _Result:
    def __init__(
        self,
        *,
        risk_level="low",
        retrieval_degraded=False,
        duplicate_assessment=None,
        proposed_actions=None,
        repo="owner/repo",
        issue_number=1,
        category=None,
    ):
        self.risk_level = risk_level
        self.retrieval_degraded = retrieval_degraded
        self.duplicate_assessment = duplicate_assessment
        self.proposed_actions = proposed_actions or []
        self.repo = repo
        self.issue_number = issue_number
        self.category = category


def _label_action(confidence=0.95, evidence=None):
    return AutomationAction(
        type="add_label",
        value="bug",
        intent=ActionIntent.ADD_CATEGORY_LABEL,
        confidence=confidence,
        rationale="Issue 描述的是可复现的软件异常",
        evidence=evidence if evidence is not None else ["错误发生在保存时"],
    )


class TestHighRiskDefers:
    def test_high_risk_defer(self):
        decision = decide_automation(
            _Result(
                risk_level="high",
                proposed_actions=[_label_action()],
            ),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff is not None
        assert decision.handoff.reason_code == DeferReasonCode.SECURITY_RISK
        assert decision.actions == []


class TestRetrievalDegradedDefers:
    def test_retrieval_degraded_defer(self):
        decision = decide_automation(
            _Result(
                retrieval_degraded=True,
                proposed_actions=[_label_action()],
            ),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.RETRIEVAL_DEGRADED


class TestDuplicateDefers:
    def test_duplicate_defer_with_handoff(self):
        decision = decide_automation(
            _Result(
                duplicate_assessment={
                    "is_duplicate": True,
                    "candidate_issue_number": 1234,
                    "evidence": ["相似标题", "相同触发条件"],
                    "rationale": "核心问题相同",
                },
                proposed_actions=[],
            ),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.DUPLICATE_UNCERTAIN
        assert "#1234" in decision.handoff.reason
        assert decision.handoff.human_task
        # 绝不自动关闭 duplicate
        assert decision.actions == []


class TestAllowAutoSemantics:
    def test_enabled_true_allow_auto_false_defers(self):
        """enabled=true, allow_auto=false -> 必须 DEFER（P0-4）。"""
        policy = _policy(
            add_category_label={
                "enabled": True,
                "min_model_confidence": 0.80,
                "require_evidence": True,
                "allow_auto": False,
            }
        )
        decision = decide_automation(
            _Result(proposed_actions=[_label_action()]),
            mode="enforce",
            calibrated_policy=policy,
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.POLICY_BLOCKED

    def test_enabled_false_allow_auto_true_defers(self):
        """enabled=false, allow_auto=true -> 必须 DEFER（P0-4）。"""
        policy = _policy(
            add_category_label={
                "enabled": False,
                "min_model_confidence": 0.80,
                "require_evidence": True,
                "allow_auto": True,
            }
        )
        decision = decide_automation(
            _Result(proposed_actions=[_label_action()]),
            mode="enforce",
            calibrated_policy=policy,
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.POLICY_BLOCKED

    def test_enabled_true_allow_auto_true_auto_executes(self):
        """enabled=true, allow_auto=true 且其他条件满足 -> AUTO_EXECUTE。"""
        policy = _policy(
            add_category_label={
                "enabled": True,
                "min_model_confidence": 0.80,
                "require_evidence": True,
                "allow_auto": True,
            }
        )
        decision = decide_automation(
            _Result(proposed_actions=[_label_action()]),
            mode="enforce",
            calibrated_policy=policy,
        )
        assert decision.disposition == AutomationDisposition.AUTO_EXECUTE


class TestPolicyBlockedDefers:
    def test_unsupported_action_defers(self):
        comment_action = AutomationAction(
            type="post_comment",
            value="已处理",
            intent=ActionIntent.POST_TECHNICAL_REPLY,
            confidence=0.99,
            rationale="技术回复",
            evidence=["证据"],
        )
        decision = decide_automation(
            _Result(proposed_actions=[comment_action]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.POLICY_BLOCKED

    def test_missing_policy_enforce_fails_closed(self):
        decision = decide_automation(
            _Result(proposed_actions=[_label_action()]),
            mode="enforce",
            calibrated_policy=None,
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.POLICY_BLOCKED

    def test_low_confidence_defers(self):
        decision = decide_automation(
            _Result(proposed_actions=[_label_action(confidence=0.50)]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert (
            decision.handoff.reason_code
            == DeferReasonCode.LOW_CALIBRATED_CONFIDENCE
        )

    def test_missing_evidence_defers(self):
        decision = decide_automation(
            _Result(proposed_actions=[_label_action(evidence=[])]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert (
            decision.handoff.reason_code == DeferReasonCode.INSUFFICIENT_EVIDENCE
        )


class TestAutoExecute:
    def test_valid_safe_action_auto_executes(self):
        decision = decide_automation(
            _Result(proposed_actions=[_label_action()]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.AUTO_EXECUTE
        assert decision.handoff is None
        assert len(decision.actions) == 1
        assert decision.actions[0].intent == ActionIntent.ADD_CATEGORY_LABEL


class TestMissingInformationIsolation:
    """P1.5：REQUEST_MISSING_INFORMATION 不能共享 category calibration。"""

    def _comment_action(self, confidence=0.99):
        return AutomationAction(
            type="post_comment",
            value="请补充日志",
            intent=ActionIntent.REQUEST_MISSING_INFORMATION,
            confidence=confidence,
            rationale="缺少复现信息",
            evidence=["缺失字段：错误日志"],
        )

    def test_request_missing_info_defers_even_when_category_calibrated(self):
        """即使 add_category_label 通过冻结 calibration，missing-info 仍 DEFER。"""
        # 构造 add_label 已校准、request_missing_info 未独立校准的 policy。
        policy = _policy(
            request_missing_information={
                "enabled": True,
                "min_model_confidence": 0.0,
                "require_evidence": True,
                "allow_auto": False,  # 未独立校准
            }
        )
        decision = decide_automation(
            _Result(proposed_actions=[self._comment_action()]),
            mode="enforce",
            calibrated_policy=policy,
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.POLICY_BLOCKED

    def test_request_missing_info_never_shares_category_confidence(self):
        """缺失信息动作的 confidence 来自独立字段，不是 category_confidence。"""
        from app.agents import workflow

        state = {
            "repo": "nodejs/node",
            "issue_number": 1,
            "category": "bug",
            "confidence": 0.99,  # category 高置信
            "missing_info_confidence": 0.0,  # 独立字段，未校准
            "missing_repro_fields": ["environment", "version"],
            "duplicate_assessment": {"is_duplicate": False},
        }
        result = workflow.prepare_actions(state)
        actions = result["proposed_actions"]
        missing_action = [
            a for a in actions
            if a["intent"] == "request_missing_information"
        ]
        assert missing_action, "应有缺失信息动作"
        # P1.5：missing-info confidence 必须是独立值（0.0），不是 category 的 0.99。
        assert missing_action[0]["confidence"] == 0.0


class TestNoAction:
    def test_no_external_action(self):
        decision = decide_automation(
            _Result(proposed_actions=[]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.NO_ACTION
        assert decision.actions == []
        assert decision.handoff is None

    def test_category_other_no_action_stays_no_action(self):
        """category=other 且无动作 -> NO_ACTION（P1.8 必须保持）。"""
        decision = decide_automation(
            _Result(category="other", proposed_actions=[]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.NO_ACTION


class TestUnsupportedActionDefers:
    """P1.8：已知语义分类但 resolver 无映射 -> DEFER，不能 NO_ACTION。"""

    def test_rust_feature_defers(self):
        decision = decide_automation(
            _Result(repo="rust-lang/rust", category="feature", proposed_actions=[]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.UNSUPPORTED_ACTION
        assert "rust-lang/rust" in decision.handoff.reason
        assert "feature" in decision.handoff.reason

    def test_rust_documentation_defers(self):
        decision = decide_automation(
            _Result(repo="rust-lang/rust", category="documentation", proposed_actions=[]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.UNSUPPORTED_ACTION

    def test_unknown_repo_defers(self):
        decision = decide_automation(
            _Result(repo="unknown/repo", category="bug", proposed_actions=[]),
            mode="enforce",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.DEFER
        assert decision.handoff.reason_code == DeferReasonCode.UNSUPPORTED_ACTION


class TestShadowMode:
    def test_shadow_records_would_auto_execute(self):
        decision = decide_automation(
            _Result(proposed_actions=[_label_action()]),
            mode="shadow",
            calibrated_policy=_policy(),
        )
        assert decision.disposition == AutomationDisposition.AUTO_EXECUTE
        assert decision.shadow is True


class TestHumanHandoffValidator:
    def test_reason_empty_rejected(self):
        with pytest.raises(ValidationError):
            HumanHandoff(
                reason_code=DeferReasonCode.INSUFFICIENT_EVIDENCE,
                reason="",
                human_task="确认缺失信息",
            )

    def test_human_task_empty_rejected(self):
        with pytest.raises(ValidationError):
            HumanHandoff(
                reason_code=DeferReasonCode.INSUFFICIENT_EVIDENCE,
                reason="#1 缺少复现步骤",
                human_task="",
            )

    def test_generic_template_rejected(self):
        with pytest.raises(ValidationError):
            HumanHandoff(
                reason_code=DeferReasonCode.LOW_CALIBRATED_CONFIDENCE,
                reason="AI 不确定，请人工审核。",
                human_task="请审核此 Issue",
            )

    def test_specific_handoff_accepted(self):
        handoff = HumanHandoff(
            reason_code=DeferReasonCode.DUPLICATE_UNCERTAIN,
            reason="检索到 #1832 与 #1947 两个强候选，但二者根因不同。",
            human_task="确认错误是在 token refresh 前还是后发生",
            evidence=["候选 #1832", "候选 #1947"],
        )
        assert handoff.reason_code == DeferReasonCode.DUPLICATE_UNCERTAIN


class TestAutomationDecisionValidator:
    def test_auto_execute_must_not_have_handoff(self):
        with pytest.raises(ValidationError):
            AutomationDecision(
                disposition=AutomationDisposition.AUTO_EXECUTE,
                policy_version="v1",
                actions=[_label_action()],
                handoff=HumanHandoff(
                    reason_code=DeferReasonCode.POLICY_BLOCKED,
                    reason="#1 有证据",
                    human_task="人工确认",
                ),
            )

    def test_defer_must_have_handoff(self):
        with pytest.raises(ValidationError):
            AutomationDecision(
                disposition=AutomationDisposition.DEFER,
                policy_version="v1",
            )

    def test_no_action_must_not_have_actions(self):
        with pytest.raises(ValidationError):
            AutomationDecision(
                disposition=AutomationDisposition.NO_ACTION,
                policy_version="v1",
                actions=[_label_action()],
            )


class TestMissingInformationRenderer:
    def test_renders_expected_fields(self):
        comment = render_missing_information_comment(
            ["environment", "version", "reproduction_steps"]
        )
        assert "运行环境" in comment
        assert "软件版本" in comment
        assert "复现步骤" in comment
        assert "请补充以下信息" in comment

    def test_deduplicates_and_maps_unknown(self):
        comment = render_missing_information_comment(
            ["version", "version", "unknown_field"]
        )
        assert comment.count("软件版本") == 1
        assert "unknown_field" in comment

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            render_missing_information_comment([])


class TestPolicyLoader:
    def test_missing_policy_file_fails_closed(self, tmp_path, monkeypatch):
        from app.automation import policy_loader

        missing = tmp_path / "missing.json"
        monkeypatch.setattr(policy_loader, "_default_policy_path", lambda: missing)
        with pytest.raises(PolicyLoaderError):
            load_calibrated_policy()

    def test_invalid_schema_fails_closed(self, tmp_path, monkeypatch):
        from app.automation import policy_loader

        bad = tmp_path / "bad.json"
        bad.write_text('{"schema_version": "9.9"}', encoding="utf-8")
        monkeypatch.setattr(policy_loader, "_default_policy_path", lambda: bad)
        with pytest.raises(PolicyLoaderError):
            load_calibrated_policy()

    def test_initial_policy_all_disabled_when_missing_version(
        self, tmp_path, monkeypatch
    ):
        from app.automation import policy_loader

        missing = tmp_path / "does-not-exist.json"
        monkeypatch.setattr(policy_loader, "_default_policy_path", lambda: missing)
        policy = load_calibrated_policy(policy_version="test-no-file")
        assert not policy.is_auto_enabled(ActionIntent.ADD_CATEGORY_LABEL)
        assert not policy.is_auto_enabled(ActionIntent.DUPLICATE_ACTION)
        assert policy.policy_version == "test-no-file"

    def test_repo_policy_file_loads(self):
        policy = load_calibrated_policy()
        assert policy.schema_version == "1.0"
        assert not policy.is_auto_enabled(ActionIntent.ADD_CATEGORY_LABEL)
        # 初始 artifact 所有 intent 都 disabled，不伪造评测指标
        for intent in (
            ActionIntent.ADD_CATEGORY_LABEL,
            ActionIntent.REQUEST_MISSING_INFORMATION,
            ActionIntent.POST_TECHNICAL_REPLY,
            ActionIntent.DUPLICATE_ACTION,
        ):
            assert not policy.is_auto_enabled(intent)
