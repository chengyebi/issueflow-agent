# Eval 数据与报告

- `datasets/triage.example.jsonl` 是六条合成样例，只用于验证 Schema、CLI 和指标计算。
- `reports/triage.example.heuristic.json` 是实际运行确定性启发式基线得到的原始报告；`publishable_model_score=false`，不得描述为 LLM 或项目真实效果。
- `datasets/duplicate*.example.jsonl` 与 `reports/duplicate.example.fake.json` 用于验证查重检索和指标管线；它们使用合成数据与 Fake Embedding，同样不能作为真实项目效果。
- `datasets/duplicate_candidates.jsonl` 和 `.csv` 是 dev/smoke 与定性压力包；保留已填写的人工标签，但不要求全部标完，也不作为未见测试集。
- `datasets/duplicate_qrels_dev.jsonl` 与 `duplicate_qrels_test.jsonl` 只包含维护者明确重复关系；同一 Duplicate Cluster 不跨 split。
- `datasets/duplicate_clusters.json` 保留关系图及泄漏/时间排除审计；`repository_snapshots.json` 记录候选语料数量和快照时间。
- Retrieval 评估正式流程：
  1. `reports/rag_baseline_dev.json` —— 默认配置（无 Prefix、`max_chunk_score`）下的 PRE-TUNING DEV baseline；
  2. `reports/rag_dev.json` —— 仅 DEV tuning 报告，包含选中配置下各方法指标与逐条预测；
  3. `reports/rag_frozen_config.json` —— DEV-only 冻结的 Prefix + Chunk aggregation；观察 TEST 前 `test_observed=false` 且无 `test_dataset_hash`；
  4. `reports/rag_primary_method.json` —— TEST 前根据预声明 DEV ranking（macro nDCG@10 → macro MRR@10 → macro Recall@5）冻结 primary retrieval method；
  5. `reports/rag_test.json` —— held-out TEST 只允许运行一次，正式 Retrieval 指标来自此处。
  - TEST 结果不得用于重新选择 query prefix、chunk aggregation 或 primary retrieval method；已冻结项在观察 TEST 后保持不变。
- 可对外引用的 Agent 指标必须使用独立人工标注集和 `live --allow-external` 实测，保留数据集 SHA-256、逐条结果、模型、Prompt、Agent 版本和运行模式。
- 未配置经过确认的模型单价时，成本指标保持 `null`。
