"""Automation Evaluation 数据集与评测 Schema。

- 从 historical_issues 的 maintainer labels 构建 category label ground truth。
- 只保留 ground truth 清晰的样本；核心映射：
    bug -> bug, feature -> enhancement, question -> question, documentation -> documentation
- 同一 Issue 有多个互相冲突的核心分类标签时，排除并记录 exclusion reason。
- 数据集可复现：保存 SHA-256、repo-aware、防止数据泄漏，
  建立新的 DEV / unseen TEST；TEST 在阈值冻结前不得查看。
"""

from pydantic import BaseModel, Field

# 真实仓库核心分类标签（统一小写键）-> category。
# 映射必须基于本仓库历史数据的实际标签体系（P1 只读统计确认）：
#   microsoft/vscode: bug, feature-request
#   nodejs/node:      question, feature request, confirmed-bug, doc
#   rust-lang/rust:   C-bug
# 反向映射（category -> 推荐 GitHub label）供预测 artifact 使用。
CORE_LABEL_MAP = {
    "bug": "bug",
    "enhancement": "feature",
    "feature": "feature",
    "question": "question",
    "documentation": "documentation",
    "c-bug": "bug",
    "confirmed-bug": "bug",
    "feature-request": "feature",
    "feature request": "feature",
    "doc": "documentation",
}

CORE_LABEL_REVERSE = {
    "bug": "bug",
    "feature": "enhancement",
    "question": "question",
    "documentation": "documentation",
}


class GroundTruthItem(BaseModel):
    repo: str
    issue_number: int
    title: str
    body: str = ""
    category: str  # bug | feature | question | documentation
    # 该 repo 中 category 对应的具体 GitHub label（来自真实维护者标签体系）。
    expected_label: str = ""
    source_labels: list[str] = Field(default_factory=list)
    state: str = "open"
    github_created_at: str = ""
    # near-duplicate group id（P1.2）；同一 group 不跨 DEV/TEST。
    group_id: int | None = None


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
