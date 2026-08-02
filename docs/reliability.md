# 任务投递与恢复

## 一致性边界

IssueFlow 不在 PostgreSQL 和 Redis 之间使用分布式事务。Webhook 事务同时写入 Delivery、Issue Event、Agent Run 和 Outbox；审核事务同时更新 Review、Command 并写入 Outbox。事务提交后尝试投递 RQ，失败时 Outbox 保持 `pending` 并按指数退避等待扫描，因此数据库已有事实不会因为 Redis 短时不可用而丢失任务。

RQ 对 Agent 和可安全重试的 GitHub 失败执行有限次数退避。HTTP 明确返回失败可标记为 `retry_safe`；连接中断可能无法确认评论是否已经创建，因此不会自动重试，避免静默发布重复评论。外部成功但本地状态更新前崩溃的 `executing` 命令也不自动重放，需要人工对账。

这套机制提供至少一次投递和本地幂等，不等于跨系统 exactly-once。

## 恢复命令

只扫描已到期且未超过次数上限的 Outbox：

```bash
docker compose run --rm backend python -m app.workers.recovery --limit 50
```

持续扫描可使用 `--loop --interval 30`，默认 Compose 不自动启动该循环，避免在本地未知状态下触发真实模型或 GitHub 调用。

API 也提供：

```text
POST /recovery/outbox/dispatch
POST /recovery/agent-runs/{id}/requeue
POST /recovery/github-commands/{id}/requeue
```

Agent Run 只有 `failed` 可重入队；GitHub Command 必须是已批准、`failed` 且 `retry_safe=true`。恢复入口不会绕过审核状态和命令类型白名单。
