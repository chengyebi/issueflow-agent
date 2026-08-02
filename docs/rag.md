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

`historical_issues` 以 `(repo, issue_number)` 唯一隔离仓库，保存标题、正文、标签、状态、GitHub 更新时间、内容哈希和索引时间。标题或正文未变化时不会重复生成 Embedding；标签和状态更新不会使向量失效。

Lexical baseline 使用 PostgreSQL 全文检索与 `pg_trgm`。Vector 检索使用 pgvector cosine distance；迁移为默认 16 维测试配置建立表达式 HNSW 索引。切换真实 Provider 和维度前，需要为目标维度新增迁移和索引，不能直接假设现有索引适用。

Hybrid 模式通过 Reciprocal Rank Fusion 合并两路结果：

```text
RRF score = sum(1 / (rrf_k + rank))
```

候选返回 lexical/vector 分数、各自排名、来源和受控证据。Embedding 失败或未配置时，Hybrid 自动降级为 lexical，并在响应中标记 `degraded=true` 和原因。

## Embedding Provider

Provider 由 `EmbeddingProvider` 协议隔离，模型名和维度来自配置：

- `disabled`：默认值，不产生网络请求或费用；
- `fake`：只用于确定性测试和合成评测；
- 其他值：明确失败，等待人工选择和实现真实 Provider。

系统不假设聊天模型 API 支持 Embedding，也不会自行下载大型模型或选择收费服务。

## Backfill 与增量同步

增量同步由 `opened`、`edited`、`closed`、`reopened` Webhook 驱动，Pull Request 事件只记录 Delivery 后即忽略。历史回填命令默认禁止网络访问，必须显式确认：

```bash
cd backend
python -m app.rag.backfill --repo owner/repo --allow-github-network
```

只有在已确认 Provider 后才能附加 `--embed`。同步运行及计数写入 `issue_sync_runs`，单条失败不会删除既有索引。

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
