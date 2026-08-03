# 长文本 Issue 检索策略

`BAAI/bge-small-en-v1.5` 的模型输入上限为 512 tokens。`head512` 保留为基线，并记录
`embedding_original_tokens`、`embedding_embedded_tokens` 和 `embedding_truncated`，不静默
截断。

Chunked 策略使用 FastEmbed 模型随附的真实 tokenizer：

- 默认 `chunk_size=384`、`chunk_overlap=64`、每条 Issue 最多 16 个 Chunk；
- 每个 Chunk 重复规范化标题，正文为空时生成含 `[empty]` 的标题 Chunk；
- 记录 original/stored/truncated token、Chunk 数及配置版本；
- 版本键包含 strategy、embedding model、tokenizer、size、overlap；
- retrieval 内容哈希不变且版本键一致时不重新分块或生成向量。

长 Query 使用同样策略拆成多个 Query Chunk，每段分别检索，再按 Issue 聚合。仅在 dev
比较 `max_chunk_score` 与 `mean_top2_chunk_score`，选择结果写入冻结配置；test 不参与选择。

当前最多保存 16 个 Chunk，极长 Issue 仍可能损失尾部内容，但损失 token 数可查询、可统计。
选择 384 而不是 512 为标题重复、特殊 token 和 overlap 留出空间，是本地 CPU 资源、召回
和可复现性之间的工程折中。
