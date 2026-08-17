"""仓库级 category -> GitHub label 解析（P1.3）。

语义：
- Agent 只输出 semantic category（bug / feature / question / documentation）。
- 具体 GitHub label 名由确定性 RepoLabelResolver 决定，Agent 不决定 label 名。
- 映射来自真实 historical_issues 的维护者标签体系（P1 只读统计确认），
  不是全局假设。
- 若某 repo 对某 category 没有经过验证的映射，返回 None；
  Policy Gate 必须 DEFER（UNSUPPORTED_ACTION），绝不允许自动创建新 GitHub label。
"""

from app.automation.models import ActionIntent

# 真实仓库 category -> concrete GitHub label。
# 来源：eval/automation/P1-DATA-REPORT.md 的只读标签统计。
# 未列出的 (repo, category) 视为无验证映射 -> resolver 返回 None。
REPO_CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "microsoft/vscode": {
        "bug": "bug",
        "feature": "feature-request",
    },
    "nodejs/node": {
        "bug": "confirmed-bug",
        "feature": "feature request",
        "question": "question",
        "documentation": "doc",
    },
    "rust-lang/rust": {
        "bug": "C-bug",
    },
}

# 语义分类集合（与 workflow.Category 对齐）。
VALID_CATEGORIES = {"bug", "feature", "question", "documentation", "other"}


def resolve_category_label(repo: str, category: str) -> str | None:
    """返回 repo 中 category 对应的 concrete GitHub label；无映射返回 None。"""
    repo_rules = REPO_CATEGORY_LABELS.get(repo)
    if repo_rules is None:
        return None
    return repo_rules.get(category)


def has_valid_mapping(repo: str, category: str) -> bool:
    return resolve_category_label(repo, category) is not None


def intent_for_label_category(category: str) -> ActionIntent:
    """category label 动作统一使用 ADD_CATEGORY_LABEL 意图。"""
    return ActionIntent.ADD_CATEGORY_LABEL
