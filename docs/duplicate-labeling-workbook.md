# VS Code 查重候选人工标注工作簿

> 已废弃为主评测工作流。保留此文档、CSV/JSONL 和 CSV 第一条既有人工标注仅供审计；
> 不再要求人工补完 60 条，也不再使用网页标注工具。正式 Retrieval Evaluation 使用
> `duplicate_qrels_dev/test.jsonl` 中的维护者明确证据。

## 文件与边界

标注文件为 `eval/datasets/duplicate_candidates.csv`。JSONL 是同一批数据的机器可读副本。候选由公开 GitHub Issue、显式重复引用和确定性文本相似度自动生成，只是待确认数据，不是真实 Ground Truth。

不要修改 Issue 编号、URL、标题、证据、标签、候选类型和快照时间。当前只填写：

- `human_label`：只能填写 `duplicate`、`non_duplicate` 或 `uncertain`；
- `human_notes`：简要写明判断依据；`uncertain` 必须说明缺失的信息；
- `split`：当前保持空白，待标注完成后由脚本按关联 Issue 分组切分，避免数据泄漏。

## 判断口径

- `duplicate`：两条 Issue 描述同一根因或同一用户可观察问题，修复一条通常同时解决另一条；
- `non_duplicate`：只共享主题、组件或关键词，但触发条件、根因或期望行为不同；
- `uncertain`：现有正文、评论和证据不足，或引用关系可能只是相关链接。

优先核对 `candidate_kind=hard_negative` 的行。这些行刻意选择标题或标签相近的 Issue，自动建议为 `non_duplicate`，但可能隐藏真实重复关系。`extracted_duplicate` 来自 `/duplicate of #...` 等显式维护者评论，可信度较高，仍需人工检查目标编号是否正确。

## 可选的定性复核

1. 用 Numbers、Excel 或支持 UTF-8 CSV 的编辑器打开 CSV；
2. 逐行打开 `query_url` 和 `proposed_duplicate_url`；
3. 阅读 `extraction_source`、`extraction_evidence` 和 `ambiguity_reason`；
4. 填写 `human_label` 与 `human_notes`；
5. 保持 UTF-8 CSV 格式保存，不要改变列名；
6. 这些填写只用于 hard-negative 定性分析，不自动生成正式 dev/test。

任何自动建议都不能用于自动关闭 Issue，也不能在人工确认前用于发布查重指标。
