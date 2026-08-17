"""Automation Evaluation 数据集与评测 Schema（v3）。

P1.9 关键修复：Ground Truth 与 Production Resolver 完全分离。

- REPO_GROUND_TRUTH_LABELS：历史维护者实际 label -> semantic category，
  仅用于解析 ground truth（expected_label 必须是 source_labels 中真实出现的 label）。
- Production Resolver（backend/app/automation/repo_labels.py）：
  semantic category -> 生产要写回的 GitHub label，仅用于预测侧。
- 两套逻辑禁止共用同一映射对象作为答案来源，避免自证循环。
"""

from pydantic import BaseModel, Field, model_validator

# Ground Truth Parser 专用：历史维护者实际 label -> semantic category。
# 键为真实 GitHub label 原文（保留大小写，如 "C-bug"），值映射到 category。
# 注意：这不是 production resolver 的反向；这是独立于 production 的 ground truth 解析。
REPO_GROUND_TRUTH_LABELS: dict[str, dict[str, str]] = {
    "microsoft/vscode": {
        "bug": "bug",
        "feature-request": "feature",
    },
    "nodejs/node": {
        "confirmed-bug": "bug",
        "feature request": "feature",
        "question": "question",
        "doc": "documentation",
    },
    "rust-lang/rust": {
        "C-bug": "bug",
    },
}

# 语义分类集合。
VALID_CATEGORIES = {"bug", "feature", "question", "documentation"}


class GroundTruthItem(BaseModel):
    repo: str
    issue_number: int
    title: str
    body: str = ""
    category: str  # bug | feature | question | documentation
    # 该 Issue 在 source_labels 中实际出现、映射到 category 的具体 GitHub label。
    expected_label: str = ""
    source_labels: list[str] = Field(default_factory=list)
    state: str = "open"
    github_created_at: str = ""
    # near-duplicate group id（P1.2）；同一 group 不跨 DEV/TEST。
    group_id: int | None = None

    @model_validator(mode="after")
    def expected_label_must_come_from_source_labels(self):
        # P1.9：expected_label 必须来源于 source_labels 的真实 label，
        # 绝不能来自 production resolver。
        if self.expected_label and self.expected_label not in self.source_labels:
            raise ValueError(
                f"expected_label={self.expected_label!r} 不在 source_labels 中: "
                f"{self.source_labels!r}"
            )
        return self


class ExcludedItem(BaseModel):
    repo: str
    issue_number: int
    reason: str


class DatasetManifest(BaseModel):
    schema_version: str = "1.0"
    dataset_name: str
    split: str  # dev | test
    dataset_hash: str
    source_repos: list[str] = Field(default_factory=list)
    item_count: int
    items: list[GroundTruthItem]
    exclusions: list[ExcludedItem] = Field(default_factory=list)


class IntentPrediction(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class CasePrediction(BaseModel):
    repo: str
    issue_number: int
    category: str
    predictions: list[IntentPrediction] = Field(default_factory=list)


class EvalRunConfig(BaseModel):
    policy_path: str
    dataset_path: str
    runner_type: str = "deterministic_heuristic"


class EvalReport(BaseModel):
    schema_version: str = "1.0"
    dataset_hash: str
    policy_version: str
    created_at: str
    eligible_count: int
    auto_execute_count: int
    defer_count: int
    no_action_count: int
    automation_coverage: float
    human_touch_rate: float
    auto_action_precision: float
    auto_action_precision_lower: float
    auto_action_precision_upper: float
    error_auto_execute_count: int
    by_intent: dict
    by_repo: dict
    by_confidence_bucket: dict
    defer_reason_distribution: dict
