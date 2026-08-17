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
from build_label_ground_truth import _group_near_duplicates, _title_jaccard  # noqa: E402


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
                "expected_label": "bug",
                "resolved_label": "feature-request",
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
                "expected_label": "bug",
                "resolved_label": "bug",
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

    def test_resolver_wrong_label_counts_as_error(self):
        """P1.4：模型 category 对但 resolver label 错 -> 必须计错。"""
        predictions = [
            {
                "repo": "microsoft/vscode",
                "issue_number": 3,
                "true_category": "feature",
                "predicted_category": "feature",
                # 模型 category 正确，但 resolver 给出错误 label（本应 feature-request）。
                "expected_label": "feature-request",
                "resolved_label": "bug",
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

    def test_generate_predictions_never_reads_true_category(self):
        """验证 generate_predictions.py 不会把 case['category'] 用作预测输入。"""
        source = (EVAL / "generate_predictions.py").read_text(encoding="utf-8")
        # true_category 只出现在写入 artifact 的行，不得出现在 _heuristic_category 内。
        heuristic_section = source.split("def _heuristic_category")[1].split("def _input_hash")[0]
        assert "true_category" not in heuristic_section
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


class TestNearDuplicateGrouping:
    """P1.2：token Jaccard near-duplicate grouping 的正确性。"""

    def _item(self, title):
        from schema import GroundTruthItem

        return GroundTruthItem(
            repo="microsoft/vscode",
            issue_number=1,
            title=title,
            category="bug",
            expected_label="bug",
            source_labels=["bug"],
            github_created_at="2026-01-01T00:00:00Z",
        )

    def test_wording_variants_group_together(self):
        """仅措辞变化的标题必须分到同组。"""
        items = [
            self._item("Cannot connect to remote server"),
            self._item("Cannot connect to the remote server"),
        ]
        groups = _group_near_duplicates(items)
        assert len(groups) == 1, "措辞变体应归入同一 group"

    def test_unrelated_titles_do_not_group(self):
        items = [
            self._item("Cannot connect to remote server"),
            self._item("Dark theme colors are wrong in menus"),
        ]
        groups = _group_near_duplicates(items)
        assert len(groups) == 2

    def test_jaccard_is_symmetric(self):
        a = "login button does not work"
        b = "login button does not work anymore"
        assert abs(_title_jaccard(a, b) - _title_jaccard(b, a)) < 1e-9


class TestGroundTruthLabelIndependence:
    """P1.9：expected_label 来自 source_labels，与 production resolver 独立。"""

    def test_ground_truth_parser_uses_source_labels(self):
        """ground truth 解析：C-bug 历史 label -> category bug + expected_label C-bug。"""
        from build_label_ground_truth import _core_category_from_labels

        category, concrete = _core_category_from_labels(
            "rust-lang/rust", ["C-bug", "T-compiler"]
        )
        assert category == "bug"
        assert concrete == "C-bug"

    def test_resolver_wrong_label_but_category_right_is_wrong(self):
        """P1.9 adversarial：resolver 返回错误 label（enhancement）即使 category 对也算 WRONG。"""
        # 模拟：历史 label feature-request -> category feature（ground truth）
        # production resolver 故意返回 enhancement（本应 feature-request）
        predictions = [
            {
                "repo": "microsoft/vscode",
                "issue_number": 10,
                "true_category": "feature",
                "predicted_category": "feature",
                "expected_label": "feature-request",  # 来自 source_labels 的真实 label
                "resolved_label": "enhancement",  # production resolver 配置错误
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


class TestGroupIdPersistence:
    """P1.7：读取实际生成的 JSONL，确认 group_id 已持久化。"""

    @staticmethod
    def _read_jsonl(path):
        items = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
        return items

    def test_dataset_jsonl_has_group_id(self):
        dataset_path = EVAL / "datasets" / "label_ground_truth_dev_v3.jsonl"
        if not dataset_path.exists():
            import pytest as _pt

            _pt.skip("v3 dataset 尚未生成")
        items = self._read_jsonl(dataset_path)
        assert items, "dataset 不应为空"
        assert all(item.get("group_id") is not None for item in items), (
            "JSONL 中存在 group_id 为 null 的 item"
        )

    def test_expected_label_from_source_labels(self):
        dataset_path = EVAL / "datasets" / "label_ground_truth_dev_v3.jsonl"
        if not dataset_path.exists():
            import pytest as _pt

            _pt.skip("v3 dataset 尚未生成")
        items = self._read_jsonl(dataset_path)
        for item in items:
            assert item["expected_label"] in item["source_labels"], (
                f"expected_label {item['expected_label']!r} 不在 source_labels 中"
            )


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
