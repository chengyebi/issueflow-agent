"""Automation evaluation 正确性 adversarial 测试（P0-1/P0-2/P0-6/P0-7）。

核心验证：
1. ground truth 与预测不同时，evaluator 必须计错（error_auto_execute_count > 0）。
   如果这个测试失败，说明 evaluator 仍有 leakage。
2. prediction 阶段绝不读取 true_category（检查 generate_predictions 源码不含该字段读取）。
3. heuristic_smoke 的 raw_confidence 固定 1.0（不伪造随机 confidence）。
4. threshold scan 在相同 prediction 上运行。
5. allow_auto=false 永远不能 auto。
6. damaged policy fail closed。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
EVAL = BACKEND.parent / "eval" / "automation"

# 让 eval/automation 内的脚本可导入。
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(EVAL))

from run_automation_eval import run_eval  # noqa: E402


def _temporary_policy(threshold: float = 0.0, *, allow_auto: bool = True) -> Path:
    from app.automation.policy_loader import ALL_INTENTS, POLICY_SCHEMA_VERSION

    rules = {}
    for intent in ALL_INTENTS:
        rules[intent.value] = {
            "enabled": True,
            "min_model_confidence": threshold,
            "require_evidence": True,
            "observed_precision": 0.9,
            "coverage": 0.5,
            "sample_count": 100,
            "allow_auto": allow_auto and intent.value == "add_category_label",
        }
    data = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_version": "test-policy",
        "created_at": "2026-08-17T00:00:00Z",
        "source_dataset_hash": "test-hash",
        "prediction_artifact_hash": "test-pred-hash",
        "model_name": "test-model",
        "prompt_version": "test-prompt",
        "rules": rules,
    }
    path = Path("/tmp/test-policy.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_predictions(rows: list[dict]) -> Path:
    path = Path("/tmp/test-predictions.jsonl")
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


class TestGroundTruthLeakage:
    def test_mismatch_prediction_counts_as_error(self):
        """ground truth=bug，文本明显是 feature -> 必须产生错误自动执行。"""
        predictions = [
            {
                "repo": "microsoft/vscode",
                "issue_number": 1,
                "true_category": "bug",
                "predicted_category": "feature",
                "predicted_label": "enhancement",
                "raw_confidence": 1.0,
                "action_intent": "add_category_label",
                "model_name": "test",
                "prompt_version": "t",
                "input_hash": "h",
                "runner_type": "heuristic_smoke",
            }
        ]
        pred_path = _write_predictions(predictions)
        policy_path = _temporary_policy(threshold=0.0, allow_auto=True)
        report = run_eval(pred_path, policy_path)
        assert report["auto_execute_count"] == 1
        assert report["error_auto_execute_count"] == 1
        assert report["auto_action_precision"] == 0.0

    def test_match_counts_as_correct(self):
        predictions = [
            {
                "repo": "microsoft/vscode",
                "issue_number": 2,
                "true_category": "bug",
                "predicted_category": "bug",
                "predicted_label": "bug",
                "raw_confidence": 1.0,
                "action_intent": "add_category_label",
                "model_name": "test",
                "prompt_version": "t",
                "input_hash": "h",
                "runner_type": "heuristic_smoke",
            }
        ]
        pred_path = _write_predictions(predictions)
        policy_path = _temporary_policy(threshold=0.0, allow_auto=True)
        report = run_eval(pred_path, policy_path)
        assert report["auto_execute_count"] == 1
        assert report["error_auto_execute_count"] == 0
        assert report["auto_action_precision"] == 1.0

    def test_generate_predictions_never_reads_true_category(self):
        """验证 generate_predictions.py 不会把 case['category'] 用作预测输入。"""
        source = (EVAL / "generate_predictions.py").read_text(encoding="utf-8")
        # true_category 只出现在写入 artifact 的行，不得出现在 _heuristic_category 内。
        assert "true_category" not in source.split("def _heuristic_category")[1].split("def _map_to_label")[0]
        # 预测函数只接受 title/body。
        assert "def _heuristic_category(title: str, body: str)" in source


class TestRandomConfidenceRemoved:
    def test_heuristic_confidence_is_fixed(self):
        """heuristic runner 的 raw_confidence 固定 1.0，不用随机数。"""
        source = (EVAL / "generate_predictions.py").read_text(encoding="utf-8")
        assert "random" not in source
        assert '"raw_confidence": 1.0' in source
        assert "heuristic_smoke" in source

    def test_eval_does_not_generate_confidence(self):
        """run_automation_eval.py 不得内部生成随机 confidence。"""
        source = (EVAL / "run_automation_eval.py").read_text(encoding="utf-8")
        assert "random" not in source
        assert "rng" not in source


class TestAllowAutoSemantics:
    def test_allow_auto_false_never_auto(self):
        from app.automation.models import (
            ActionIntent,
            AutomationAction,
            AutomationDisposition,
        )
        from app.automation.policy import decide_automation

        # enabled=true, allow_auto=false -> DEFER
        policy_path = _temporary_policy(allow_auto=False)
        import json as _json
        from app.automation.policy_loader import load_calibrated_policy

        policy = load_calibrated_policy(policy_path)
        action = AutomationAction(
            type="add_label",
            value="bug",
            intent=ActionIntent.ADD_CATEGORY_LABEL,
            confidence=0.99,
            rationale="r",
            evidence=["e"],
        )
        result = _FakeResult([action])
        decision = decide_automation(result, mode="enforce", calibrated_policy=policy)
        assert decision.disposition == AutomationDisposition.DEFER


class _FakeResult:
    def __init__(self, actions):
        self.risk_level = "low"
        self.retrieval_degraded = False
        self.duplicate_assessment = None
        self.proposed_actions = actions
        self.repo = "owner/repo"
        self.issue_number = 1


class TestDamagedPolicyFailClosed:
    def test_empty_dataset_hash_for_enforce_rejected(self):
        from app.automation.policy_loader import (
            ALL_INTENTS,
            PolicyLoaderError,
            load_calibrated_policy,
        )

        rules = {}
        for intent in ALL_INTENTS:
            rules[intent.value] = {
                "enabled": True,
                "min_model_confidence": 0.0,
                "require_evidence": True,
                "observed_precision": 0.9,
                "coverage": 0.5,
                "sample_count": 100,
                "allow_auto": True,
            }
        data = {
            "schema_version": "1.0",
            "policy_version": "bad",
            "created_at": "2026-08-17T00:00:00Z",
            "source_dataset_hash": "",
            "rules": rules,
        }
        path = Path("/tmp/bad-policy.json")
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(PolicyLoaderError):
            load_calibrated_policy(path, for_enforce=True)

    def test_missing_prediction_artifact_hash_for_enforce_rejected(self):
        from app.automation.policy_loader import (
            ALL_INTENTS,
            PolicyLoaderError,
            load_calibrated_policy,
        )

        rules = {}
        for intent in ALL_INTENTS:
            rules[intent.value] = {
                "enabled": True,
                "min_model_confidence": 0.0,
                "require_evidence": True,
                "observed_precision": 0.9,
                "coverage": 0.5,
                "sample_count": 100,
                "allow_auto": True,
            }
        data = {
            "schema_version": "1.0",
            "policy_version": "bad",
            "created_at": "2026-08-17T00:00:00Z",
            "source_dataset_hash": "hash",
            "rules": rules,
        }
        path = Path("/tmp/bad-policy2.json")
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(PolicyLoaderError):
            load_calibrated_policy(path, for_enforce=True)
