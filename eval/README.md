# Eval 数据与报告

- `datasets/triage.example.jsonl` 是六条合成样例，只用于验证 Schema、CLI 和指标计算。
- `reports/triage.example.heuristic.json` 是实际运行确定性启发式基线得到的原始报告；`publishable_model_score=false`，不得描述为 LLM 或项目真实效果。
- `datasets/duplicate*.example.jsonl` 与 `reports/duplicate.example.fake.json` 用于验证查重检索和指标管线；它们使用合成数据与 Fake Embedding，同样不能作为真实项目效果。
- `datasets/duplicate_candidates.jsonl` 和 `.csv` 是 dev/smoke 与定性压力包；保留已填写的人工标签，但不要求全部标完，也不作为未见测试集。
- `datasets/duplicate_qrels_dev.jsonl` 与 `duplicate_qrels_test.jsonl` 只包含维护者明确重复关系；同一 Duplicate Cluster 不跨 split。
- `datasets/duplicate_clusters.json` 保留关系图及泄漏/时间排除审计；`repository_snapshots.json` 记录候选语料数量和快照时间。
- `reports/rag_dev.json` 用于选择 Prefix 与 Chunk 聚合；`rag_frozen_config.json` 生成后才允许运行一次 test，正式 Retrieval 指标来自 `rag_test.json`。
- 可对外引用的 Agent 指标必须使用独立人工标注集和 `live --allow-external` 实测，保留数据集 SHA-256、逐条结果、模型、Prompt、Agent 版本和运行模式。
- 未配置经过确认的模型单价时，成本指标保持 `null`。
