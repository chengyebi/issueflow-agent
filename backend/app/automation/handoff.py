"""人工接管相关的确定性模板与构建辅助。

原则：LLM 只负责结构化判断“缺什么”，真正对外写出的低风险请求补充信息
由确定性代码生成，减少幻觉、token、审核成本与随机措辞。
"""

from app.automation.models import DeferReasonCode, HumanHandoff

FIELD_LABELS = {
    "environment": "运行环境",
    "version": "软件版本",
    "reproduction_steps": "复现步骤",
    "expected_behavior": "预期结果",
    "actual_behavior": "实际结果",
    "error_logs": "错误日志",
}


def render_missing_information_comment(fields: list[str]) -> str:
    normalized = []
    seen = set()

    for field in fields:
        if field in seen:
            continue
        seen.add(field)
        normalized.append(FIELD_LABELS.get(field, field))

    if not normalized:
        raise ValueError("缺失信息列表不能为空")

    items = "\n".join(f"- {item}" for item in normalized)

    return (
        "感谢反馈。为了进一步定位这个问题，请补充以下信息：\n\n"
        f"{items}\n\n"
        "补充后我们可以继续定位。"
    )


def build_missing_information_handoff(
    issue_number: int,
    missing_fields: list[str],
    *,
    reason_code: DeferReasonCode = DeferReasonCode.INSUFFICIENT_EVIDENCE,
) -> HumanHandoff:
    """当 Agent 判断 Issue 缺少复现信息时，构造一个最小、具体的人工任务。

    人工只需要确认 Agent 对缺失字段的判断是否正确即可。
    """
    labels = [FIELD_LABELS.get(f, f) for f in missing_fields]
    fields_text = "、".join(labels) if labels else "相关信息"
    return HumanHandoff(
        reason_code=reason_code,
        reason=(
            f"当前 Issue #{issue_number} 缺少 {fields_text}，"
            "无法可靠地进一步定位或自动处理，需要确认缺失信息判断。"
        ),
        human_task=(
            f"请确认当前 Issue #{issue_number} 是否确实缺少 {fields_text}，"
            "以及补充后是否足以判断根因。"
        ),
        evidence=[f"缺失字段：{'、'.join(labels)}"],
        already_checked=[
            "已完成基础分类",
            "已完成缺失信息字段识别",
        ],
    )


def build_duplicate_handoff(
    issue_number: int,
    candidate_issue_number: int,
    evidence: list[str],
    rationale: str,
) -> HumanHandoff:
    """duplicate 分支的 enriched handoff。

    当前 Retriever 离线覆盖率不足以支持自动重复处理，但检索结果
    仍然真实减少人工搜索工作：人只需要做一个局部判断。
    """
    return HumanHandoff(
        reason_code=DeferReasonCode.DUPLICATE_UNCERTAIN,
        reason=(
            f"系统检索到疑似重复候选 #{candidate_issue_number}，"
            "但当前重复检索尚未达到自动执行所需的可靠性，"
            "需要确认是否与当前 Issue 属于同一核心问题。"
        ),
        human_task=(
            f"只需确认当前 Issue #{issue_number} 与 #{candidate_issue_number} "
            "是否属于同一根因。"
        ),
        evidence=evidence,
        already_checked=[
            "已完成历史 Issue 检索",
            "已完成候选语义比较",
            "已完成基础分类",
        ],
    )
