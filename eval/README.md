# Eval 数据与报告

- `datasets/triage.example.jsonl` 是六条合成样例，只用于验证 Schema、CLI 和指标计算。
- `reports/triage.example.heuristic.json` 是实际运行确定性启发式基线得到的原始报告；`publishable_model_score=false`，不得描述为 LLM 或项目真实效果。
- 可对外引用的 Agent 指标必须使用独立人工标注集和 `live --allow-external` 实测，保留数据集 SHA-256、逐条结果、模型、Prompt、Agent 版本和运行模式。
- 未配置经过确认的模型单价时，成本指标保持 `null`。
