# 历史 Issue 混合检索与查重

## 链路与安全边界

```text
GitHub Backfill / Issue Webhook
  -> Pull Request 过滤
  -> repo + issue_number 幂等更新
  -> content_hash 判断内容变化
  -> lexical / vector 检索
  -> RRF 融合 Top-K
  -> 结构化重复判断
  -> 人工审核建议
```

查重结论只形成审核建议，不会自动关闭 Issue，也不会绕过现有命令白名单和人工审核。Issue 标题、正文和候选证据均按不可信内容处理。高风险 Issue 仍不生成公开 GitHub 写操作。

## 数据与索引

`historical_issues` 以 `(repo, issue_number)` 唯一隔离仓库，保存标题、正文、标签、状态、GitHub 创建/更新时间、内容哈希和索引时间。正式检索输入仅包含标题和正文；标签只用于展示，避免把维护者事后添加的 duplicate 标签泄漏给查询。标题/正文内容哈希未变化时不会重复生成向量或 Chunk。

`historical_issue_chunks` 保存真实 tokenizer 切分的 384 维向量。默认每段 384 tokens、重叠 64、最多 16 段，且每段重复标题；父 Issue 记录原始、保存和截断 token 数以及完整版本键。详见 [长文本策略](rag-long-document-strategy.md)。

Lexical baseline 使用 PostgreSQL 全文检索与 `pg_trgm`。Vector 检索使用 pgvector cosine distance；迁移分别保留 16 维 Fake 测试索引并新增 384 维 BGE HNSW 索引。

Hybrid 模式通过 Reciprocal Rank Fusion 合并两路结果：

```text
RRF score = sum(1 / (rrf_k + rank))
```

候选返回 lexical/vector 分数、各自排名、来源和受控证据。Embedding 失败或未配置时，Hybrid 自动降级为 lexical，并在响应中标记 `degraded=true` 和原因。

## Embedding Provider

Provider 由 `EmbeddingProvider` 协议隔离，模型名和维度来自配置：

- `fastembed`：Docker CPU 内运行 `BAAI/bge-small-en-v1.5`，不把 Issue 内容发送到外部服务；
- `disabled`：禁用向量检索，不产生模型下载；
- `fake`：只用于确定性测试和合成评测；
- 其他值：明确失败，不会静默切换到云端服务。

模型缓存位于持久化 Volume `fastembed_cache`，容器路径为 `/var/cache/issueflow/fastembed`。首次启动允许下载；缓存完成后设置 `EMBEDDING_LOCAL_FILES_ONLY=true` 可禁止联网加载。启动探针会实际生成向量并校验 384 维，不一致时终止启动，禁止写入数据库。

### 确定性文本表示

1. 使用 Unicode NFKC；统一换行并压缩行内空白；
2. 空正文显式写为 `[empty]`；
3. 标签不进入检索文本；
4. 固定字段顺序为 `Title`、`Body`；
5. 表示版本记录为 `issue-title-body-v2`。

该模型最大输入为 512 tokens。FastEmbed 会在 512 tokens 处截断；系统在推理前用同一 tokenizer 统计未截断 token 数，并把原始 token 数、实际输入 token 数、最大值和 `embedding_truncated` 写入数据库。查询响应也返回截断观测，避免静默丢失信息。

Issue-to-Issue 相似度默认不添加 Query Prefix。开发集同时比较无 Prefix 与 `Represent this sentence for searching relevant passages: `；结果冻结后才运行测试集，不用 test 反向选参数。

所有离线评测还传入 `query_created_at`，SQL 强制候选同仓库、不是 Query 自身且创建时间更早。数据泄漏与 Cluster split 规则见 [评测方法](rag-evaluation-methodology.md) 和 [泄漏边界](rag-data-leakage.md)。

## Backfill 与增量同步

增量同步由 `opened`、`edited`、`closed`、`reopened` Webhook 驱动，Pull Request 事件只记录 Delivery 后即忽略。历史回填命令默认禁止网络访问，必须显式确认：

```bash
cd backend
python -m app.rag.backfill --repo owner/repo --allow-github-network
```

附加 `--embed` 后使用已配置的本地 Provider。`--max-issues` 是强制数量上限；同步运行及计数写入 `issue_sync_runs`，单条失败不会删除既有索引。

## 查询 API

```text
GET /historical-issues/search?repo=owner/repo&query=login%20timeout&mode=hybrid&top_k=5
```

`mode` 支持 `lexical`、`vector` 和 `hybrid`。默认 Provider 未配置时，`vector` 不可用，`hybrid` 会安全降级。

## 可复现合成评测

```bash
cd backend
python -m app.rag.eval_cli \
  --dataset ../eval/datasets/duplicate.example.jsonl \
  --corpus ../eval/datasets/duplicate.corpus.example.jsonl \
  --output ../eval/reports/duplicate.example.fake.json
```

报告比较 Recall@1、Recall@5、MRR@10、Precision@1、重复判断 Precision/Recall/F1 和检索 P50/P95。示例报告使用 Fake Embedding 与合成数据，`publishable_model_score=false`，不能作为真实 Provider 或真实项目效果。
