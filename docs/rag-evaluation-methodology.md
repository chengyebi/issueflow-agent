# 历史 Issue 查重检索评测方法

## 真值与快照

评测仓库为 `microsoft/vscode`、`nodejs/node`、`rust-lang/rust`。Ground Truth 优先使用当前
有效的 duplicate 事件；其次使用维护者身份明确写出的 duplicate comment。普通用户猜测和
只有 duplicate 标签但没有目标编号的 Issue 不进入正式正样本；unmark 会取消旧关系。

有效关系构成无向 Duplicate Cluster。对 Query A，所有创建时间早于 A 的同簇 Issue 都是
相关文档，避免 A→B→C 链只认可单一 Target。数据文件：

- `duplicate_qrels_dev.jsonl` / `duplicate_qrels_test.jsonl`；
- `duplicate_clusters.json`：Cluster 与排除审计；
- `repository_snapshots.json`：数量上限、同步计数和快照时间。

## 对照与冻结

候选语料按仓库隔离且严格早于 Query。dev 比较 Query Prefix 和 Chunk 聚合，冻结后才运行
test。正式对照为 lexical、vector_head512、vector_chunked、hybrid_head512_rrf、
hybrid_chunked_rrf。无 dev 优势时，较简单的 no-prefix/max 策略作为确定性 tie-breaker。

指标为 Recall@1/5/10、MRR@10、nDCG@10、P50/P95；同时保存命中 Query 数。置信区间用
固定随机种子的 Query 级 Bootstrap（至少 2000 次）。每仓库单独报告，再计算仓库 Macro。

Vector 同时运行 exact cosine 和 HNSW。Exact 在事务内关闭 index scan；HNSW 使用
`<=>` 与 `vector_cosine_ops`。报告 HNSW 相对 Exact 的 Top-K recall、完整顺序一致率和延迟，
当前规模没有收益时如实保留结果。

## 负样本边界

正式报告是 Retrieval Evaluation，不宣称完整 Duplicate Classification F1。Easy negatives
仅报告简单误报率；主题相近 hard negatives 是定性 stress set，未获得维护者证据时允许
uncertain，不能伪装成 Ground Truth。
