# IssueFlow Agent 升级计划

## 基线现状

当前链路已经覆盖 GitHub Webhook HMAC 验签、Delivery 去重、PostgreSQL 事务入库、Redis/RQ 异步任务、LangGraph 分诊、人工审核以及 GitHub 标签和评论写回。主要工程缺口是入口模块职责集中、已有数据卷缺少持续迁移入口、自动化测试不足、队列投递存在一致性窗口，并且没有 Trace、Eval、历史 Issue 检索和可视化控制台。

## 目标架构

```text
GitHub Webhook
  -> API routers -> application services -> PostgreSQL
                                      -> transactional outbox -> RQ workers
  -> LangGraph workflow / multi-agent -> node traces -> review task
  -> human decision -> command outbox -> GitHub REST API

Historical Issues -> lexical + vector retrieval -> RRF -> duplicate review
Repository Memory -> versioned, sourced, repo-scoped rules
React Console -> review / trace / eval / memory APIs
```

后端保留 `app.main:app`，内部按 `api`、`core`、`db`、`models`、`services`、`agents`、`workers` 拆分。PostgreSQL 是业务事实来源，Redis 只承担任务传递。所有外部写操作仍然受命令白名单和人工审核约束。

## 数据模型演进

1. 保留 `webhook_deliveries`、`issue_events`、`agent_runs`、`review_tasks`、`github_commands`。
2. 为 Agent Run 增加 `trace_id`、模型/Prompt/Agent 版本、耗时、Token、重试与错误分类。
3. 新增节点级 Trace、事务 Outbox 与 Eval Report。Webhook、Issue Event、Agent Run 与 `agent_run` Outbox 在同一事务提交；审核决定、命令状态与 `review_commands` Outbox 同理。
4. 后续新增历史 Issue、向量索引、重复判断、仓库记忆和人工反馈表；所有仓库级数据以 `repo` 隔离。

## 迁移策略

使用 Alembic 维护仅前滚迁移。首个迁移以幂等 DDL 接管已有初始化脚本创建的数据卷；Compose 通过独立 `migrate` 服务在 backend 和 worker 启动前执行 `upgrade head`。迁移不得删除表、清空数据或重建数据卷。

## 阶段计划

1. 模块化后端，建立 Alembic、pytest、Mock 测试与 CI。
2. 增加 Trace、离线 Eval、有限重试、Outbox 和恢复入口。
3. 同步历史 Issue，实现 lexical、vector、RRF 与查重审核。
4. 建立仓库记忆与 React 审核/观测控制台。
5. 对照 workflow 与 multi-agent，完成故障测试、可复现指标和文档。

## 主要风险与控制

- **外部操作重复**：数据库幂等键只能约束本地记录；GitHub 成功但本地状态未更新仍需恢复对账。
- **队列一致性**：数据库提交后 Redis 投递可能失败，使用事务 Outbox 和可恢复扫描消除任务遗失。
- **Outbox 取舍**：当前采用“数据库至少一次投递 + 业务幂等键”，而不是跨 PostgreSQL/Redis 的分布式事务。扫描器会恢复未确认投递；GitHub 外部成功但本地更新前崩溃的窗口仍需后续对账，不能宣称严格 exactly-once。
- **模型不确定性**：结构化 Schema、固定 Prompt 版本、离线标注集、人工审核共同约束。
- **敏感数据扩散**：Trace 只保存字段摘要和受控输出，不保存签名、Token 或完整原始 Payload。
- **Embedding 选择**：Provider 抽象化；未经确认不下载大型模型、不假设 Chat API 提供 Embedding、不调用收费服务。
- **指标误读**：报告同时保存数据集、运行模式、版本和原始逐条结果；示例或 Mock 结果不得宣传为真实模型成绩。
