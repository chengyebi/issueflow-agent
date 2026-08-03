# 历史 Issue 查重标注指南

## 数据层级

- `maintainer-grounded positives`：当前有效的 `marked_as_duplicate` 事件，或
  `MEMBER`、`OWNER`、`COLLABORATOR` 明确写出的 `Duplicate of #N`。正式检索指标只使用这一层。
- `easy negatives`：明显不相似的简单负样本，只报告误报率，不代表困难场景。
- `qualitative hard-negative stress set`：主题相近的压力样本，允许
  `duplicate`、`non_duplicate`、`uncertain`，无维护者证据时不作为绝对真值。
- `uncertain samples`：证据不足或冲突的审计样本，不进入正式指标。

现有 `duplicate_candidates.csv/jsonl` 中的 30 条显式关系属于 dev/smoke 数据，
可调试 Chunk、Prefix 和 RRF，但不是未见测试集。CSV 中已有人工标注保留；不再要求把
60 条全部人工填完，也不再使用本地网页标注脚本。

## 标注单位

每条样本包含目标 Issue、同仓库候选 Issue、是否重复的人工标签，以及重复时的标准 Issue 编号。跨仓库内容不作为重复候选。

## 判断原则

- `duplicate=true`：两条 Issue 描述的是同一根因或同一用户可观察问题，解决其中一条通常同时解决另一条。
- `duplicate=false`：仅共享关键词、组件或症状，但根因、触发条件或期望行为不同。
- 信息不足时标记为待复核，不纳入发布指标；不要强制猜测。
- 安全漏洞、凭据和隐私内容必须脱敏，不能把完整敏感正文放入数据集。

标注时记录简短理由和证据片段，证据应足以复核，但不复制完整 Issue Payload。推荐由两名标注者独立判断，对分歧样本进行仲裁，并保留数据集版本和 SHA-256。

## 评测口径

- 检索：Recall@1/5/10、MRR@10、nDCG@10；
- 当前不发布完整 duplicate classification F1；困难负样本只做定性压力分析；
- 性能：每种检索模式的 P50/P95；
- 对照：lexical、vector、hybrid RRF，以及明确启用后的可选 reranker。

报告必须保存逐条预测、配置、Provider、模型、维度和运行时间。Fake Provider 与合成样本只能验证管线，不能对外声称为真实模型成绩。
