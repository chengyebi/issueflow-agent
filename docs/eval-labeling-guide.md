# IssueFlow Eval 标注指南

## 目的

离线评测用于比较固定数据集上的 Agent 版本、Prompt 版本与运行模式。`triage.example.jsonl` 是合成样例，只验证 Schema 和评测管线，任何输出都不能作为真实模型成绩。可对外引用的指标必须来自人工确认标签的独立数据集，并保留原始逐条结果。

## JSONL Schema

每行是一条独立 JSON，包含唯一 `id`、`input`、`expected` 和 `metadata`。输入字段为 `repo`、`issue_number`、`title`、`body`；标签字段为 `category`、`priority`、`risk_level`。`metadata.source` 应说明数据来自真实历史 Issue、人工构造边界样本还是合成示例。

## 标注规则

- `category` 只能为 `bug`、`feature`、`question`、`documentation`、`other`。
- `priority` 只能为 `low`、`medium`、`high`、`critical`，依据影响范围、阻断程度和时间敏感性判断，不等同于表达语气。
- `risk_level=high` 用于漏洞利用、认证绕过、密钥泄露、隐私数据或危险执行操作。拿不准时先标记待复核，不用猜测填充。
- 标注者不得参考待评测模型的预测。建议两人独立标注，高风险分歧必须仲裁，并在 `notes` 记录理由。
- 数据集拆分后冻结；修改标签或样本必须生成新版本并记录数据集 SHA-256。

## 指标解释

- Accuracy 与 Macro-F1 衡量分类；Macro-F1 防止大类掩盖小类。
- 高风险 Recall 优先关注漏报；当数据集中没有高风险正例时结果为 `null`，不得写成 0 或 100%。
- Structured Output 成功率与 Agent 成功率分别衡量解析和端到端执行。
- P50/P95 来自逐条实测耗时；Token 来自模型响应元数据；未配置已确认单价时成本为 `null`。

## 运行

```bash
cd backend
python -m app.eval.cli \
  --dataset ../eval/datasets/triage.example.jsonl \
  --output ../eval/reports/triage-example-heuristic.json \
  --runner heuristic
```

真实模型会产生外部调用，必须显式确认后使用：

```bash
python -m app.eval.cli --dataset <labeled.jsonl> --output <report.json> \
  --runner live --allow-external
```
