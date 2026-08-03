# 查重评测的数据泄漏边界

正式查询只使用 Issue 在创建入口可获得的 `title` 和 `body`。下列信息只用于构造或
审计 Ground Truth，绝不进入 lexical query、query embedding 或重复判断上下文：

- 评论与 `/duplicate of #N` 文本；
- `marked_as_duplicate`、`unmarked_as_duplicate` 事件；
- duplicate 标签、关闭原因及维护者后续分类标签；
- Ground Truth 目标编号；
- `proposed_label`、`human_label`。

若 query 的标题或正文自身出现 Ground Truth 目标编号，记录
`query_title_or_body_contains_target` 和 `leakage_risk=true`，保留在
`duplicate_clusters.json` 的审计区，但排除正式 dev/test。

候选必须和 Query 同仓库、不是自身，并满足
`candidate.github_created_at < query_created_at`。目标创建时间不早于 Query 的关系不进入
qrels。Duplicate Cluster 作为 split 单位；同一 Issue 和同一 Cluster 不跨 dev/test。
测试集只在 Prefix、Chunk 聚合和其他参数由 dev 冻结后运行一次。
