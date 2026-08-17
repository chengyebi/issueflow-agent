# IssueFlow Agent

[![CI](https://github.com/chengyebi/issueflow-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/chengyebi/issueflow-agent/actions/workflows/ci.yml)

面向 GitHub 仓库维护场景的 Issue 智能分诊与**选择性自动化**系统。

IssueFlow 通过 GitHub Webhook 接收 Issue 事件，用 LangGraph 编排大模型分析流程，生成分类、风险判断、缺失复现信息检查、标签/回复草案，并从历史 Issue 中检索相似问题辅助查重。普通、低风险且满足冻结可靠性策略的 Issue 由受控 Worker **自动执行**；无法满足可靠性要求的异常案例携带明确的 defer reason、证据与最小人工任务进入 **Exception Queue** 人工接管。

> 自动处理是正常路径，人工接管是异常路径。

This project focuses on **selective automation**: **the model proposes, the policy constrains, and a worker executes what the frozen policy proves reliable; the rest is handed to a human with a minimal task.**

- 项目类型：工作流型 Agent（LangGraph workflow），非无约束通用 Agent
- 核心原则：确定性 Policy Gate 决定哪些动作可自动执行；人工只处理 Agent 无法可靠判断的最小问题
- Rollout：默认 `AUTOMATION_MODE=shadow`；`off` / `shadow` / `enforce` 三档，enforce 缺少冻结策略时 fail-closed
- 评估状态：Retrieval Evaluation 与 Label Automation Calibration 均已按冻结协议完成 held-out TEST；自动标签已有冻结策略与可发布离线指标。正式默认 rollout 仍保持 `AUTOMATION_MODE=shadow`，不会默认无人写入 GitHub

## Evaluation Snapshot

### Retrieval

历史 Issue 查重检索在 **maintainer-derived positive duplicate-relation retrieval benchmark** 上按冻结协议评测：

- 语料快照 **6485** 条已入库历史 Issue；真值仅来自维护者明确操作，共 **164** 条 duplicate-relation qrels（DEV **74** / TEST **90**），按 **147** 个 Duplicate Cluster 完全隔离；
- **frozen primary（TEST 前冻结）= `vector_chunked`**，TEST macro：Recall@5=**0.6835**、MRR@10=**0.6009**、nDCG@10=**0.6269**；
- `vector_head512` 的 TEST macro nDCG point estimate **0.6353** 小幅高于 frozen primary，但 TEST 不用于回选方法。

完整五方法对比表见 [检索评测](#检索评测)。

### Label Automation

自动标签使用真实维护者标签作为 Ground Truth，并严格执行 **DEV 冻结 → unseen TEST 唯一一次正式评测**：

- DEV **2011** 条、TEST **506** 条；按 repo + category 分层时间切分，near-duplicate group 不跨 split；
- DEV 上冻结自动执行 threshold **0.92**，冻结策略为 `eval/automation/policy.label.frozen.json`；
- unseen TEST 上 **198 / 506** 条进入自动标签集合，coverage **39.13%**；
- auto-action precision **93.94%（186 / 198）**，Wilson 95% CI **[89.71%, 96.50%]**；
- TEST structured-output failure **0**；预测为 high-risk 的 Issue 全部 DEFER；
- 当前冻结策略仅允许 `add_category_label` AUTO_EXECUTE；`request_missing_information`、`post_technical_reply`、`duplicate_action` 均未获得自动执行授权。

这些数字只衡量 **label auto-action**，不等价于完整 Agent 准确率、duplicate classification precision 或端到端成功率。

完整自动标签协议、成本和分桶结果见
[`eval/automation/FINAL-LABEL-AUTOMATION-REPORT.md`](eval/automation/FINAL-LABEL-AUTOMATION-REPORT.md)。

---

## 目录

- [Evaluation Snapshot](#evaluation-snapshot)
- [为什么需要 IssueFlow](#为什么需要-issueflow)
- [核心设计](#核心设计)
- [系统总览](#系统总览)
- [事件接入与事务一致性](#事件接入与事务一致性)
- [Agent 工作流](#agent-工作流)
- [选择性自动化（Selective Automation）](#选择性自动化selective-automation)
- [人工接管与 GitHub 写回](#人工接管与-github-写回)
- [历史 Issue 检索](#历史-issue-检索)
- [检索评测](#检索评测)
- [可观测性](#可观测性)
- [安全与可靠性](#安全与可靠性)
- [API 一览](#api-一览)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置 GitHub Webhook](#配置-github-webhook)
- [使用方式](#使用方式)
- [验证路径](#验证路径)
- [当前边界](#当前边界)
- [Roadmap](#roadmap)
- [文档与评估产物](#文档与评估产物)

---

## 为什么需要 IssueFlow

GitHub 仓库的 Issue 维护通常是重复且耗时的：判断类型、评估优先级、核对复现信息、回复提问、打标签、排查是否已有重复 Issue。

直接让大模型修改 GitHub 有明显风险：

- 模型输出不确定，可能生成错误标签或不合适的公开回复；
- Issue 正文属于外部不可信输入，不能当作可执行指令；
- 安全漏洞不适合自动公开处理；
- 外部 API 调用需要权限、状态与失败记录。

IssueFlow 把整个过程拆成一条受约束的流水线，而不是把钥匙直接交给模型：

```text
事件接入
→ 验签 / 去重 / 事务入库
→ 后台 Agent 分析
→ 确定性 Policy Gate
   ├─ AUTO_EXECUTE → policy 授权 → github_commands Outbox → Worker → GitHub
   ├─ DEFER        → Exception Queue → Human
   └─ NO_ACTION    → Done
→ 保存决策、授权与执行结果
```

## 核心设计

| 原则 | 含义 |
|---|---|
| 模型提出建议 | 模型只生成结构化分析结果和动作意图（intent/confidence/evidence） |
| 策略约束能力 | 确定性 Policy Gate 决定动作是否可自动执行；命令白名单只允许 `add_label` 与 `post_comment` |
| 选择性自动化 | 普通低风险动作 AUTO_EXECUTE；异常案例 DEFER 进 Exception Queue 人工接管；无动作 NO_ACTION 结束 |
| 冻结策略约束 | 自动执行只由经过离线评测冻结的 policy artifact 授权；raw LLM confidence 只是 signal 不是可信度 |
| 人工只做最小任务 | 每条 handoff 含 reason_code、具体 reason、最小 human_task、evidence 与 already_checked |
| PostgreSQL 是事实来源 | 状态与错误保存在数据库，Redis 只做任务队列 |
| 可追踪 | 每条 Agent 运行记录节点级 Trace、Token、估算成本与 automation decision |

模型不能：关闭 Issue、删除评论、修改代码、创建 PR、执行 Issue 文本中的脚本、绕过 Policy Gate / 授权边界调用 GitHub。

## 系统总览

```mermaid
flowchart TB
    subgraph Ext["外部"]
        GH["GitHub"]
        LLM["LLM API"]
    end

    subgraph App["Docker Compose 应用"]
        API["FastAPI backend :8000"]
        DB[("PostgreSQL + pgvector")]
        RD[("Redis 7")]
        Q["RQ Queue"]
        AW["Agent Worker"]
        PGATE["Deterministic Policy Gate"]
        CW["Command Worker"]
        UI["Exception Queue / Review Console"]
    end

    GH -->|"issues webhook"| API
    API -->|"事务：delivery + event + run + outbox"| DB
    API -->|"dispatch agent_run"| Q

    Q --> AW
    AW -->|"LangGraph analysis"| LLM
    AW --> PGATE
    PGATE -->|"decision / authorization / command state"| DB

    PGATE -->|"AUTO_EXECUTE: policy command + github_commands Outbox"| DB
    AW -->|"after commit: dispatch github_commands"| Q

    PGATE -->|"DEFER: review_task + human proposed commands"| DB
    UI -->|"read pending / approve / reject"| API
    API -->|"read / update review state"| DB
    API -->|"approved: review_commands Outbox"| DB
    API -->|"after commit: dispatch review_commands"| Q

    PGATE -->|"NO_ACTION"| DONE["Done"]

    Q --> CW
    CW -->|"authorized add_label / post_comment"| GH
    CW -->|"executed / failed"| DB

    API -->|"search / trace / metrics"| DB
    RD -->|"RQ backend"| Q
```

| 组件 | 职责 |
|---|---|
| FastAPI backend | 接收 Webhook、提供审核/检索/观测 API、静态托管 Review Console |
| PostgreSQL | 业务事实、审核状态、AutomationDecision、GitHub Command、检索向量（pgvector）与 Trace |
| Redis + RQ | 任务队列；Agent 与 Command 执行任务从中消费 |
| Agent Worker | 执行 LangGraph 分析；完成后按确定性 Policy Gate 路由，并在事务提交后派发后续 Outbox |
| Deterministic Policy Gate | 使用冻结 policy 对全部 proposed actions 做 fail-closed 决策，不再调用 LLM |
| Command Worker | 只执行持有合法 `policy` 或 `human` 授权的 GitHub 写操作 |
| Review Console | 展示需要人工处理的 Review / Exception Queue；enforce 下主要承接 DEFER，shadow 下也承接真实动作的人工作业 |
| LLM API | 通过 OpenAI-compatible 接口配置，默认 DeepSeek |

> 上图按 `enforce` 的真实 side-effect 路由展示。`shadow` 模式仍记录 Policy Gate 裁定，但除 `NO_ACTION` 外，真实动作继续走人工审核路径。

## 事件接入与事务一致性

Webhook 端只做三件事：验签、去重、入库后尽快返回。分析等耗时工作交给 Worker 异步执行，避免外部服务失败拖垮入口请求。

支持 GitHub `issues` 事件，action 白名单为 `opened` / `edited` / `closed` / `reopened`。Pull Request 事件只记录 Delivery 后忽略。签名使用 `X-Hub-Signature-256` HMAC-SHA256 校验；`X-GitHub-Delivery` 唯一约束防止重复入库。

### 单事务交付

收到事件后，后端在**一个数据库事务**里完成：

```text
INSERT webhook_deliveries (delivery_id 幂等)
INSERT issue_events
INSERT agent_runs
INSERT outbox_events (event_type = 'agent_run')
必要时 INSERT outbox_events (event_type = 'issue_index')
```

事务提交后再投递 RQ。投递失败时 Outbox 记录保持 `pending`，由扫描任务按指数退避重试，因此数据库中的任务不会因为 Redis 短时不可用而丢失。这套机制提供**至少一次投递**和本地幂等，不等于跨系统 exactly-once。

Outbox 事件类型：`agent_run`（触发 Agent 分析）、`github_commands`（AUTO_EXECUTE 后派发 policy 授权命令）、`review_commands`（人工批准后派发命令）、`issue_index`（后台索引历史 Issue）。

## Agent 工作流

Agent 是代码预先定义路径的工作流型 Agent，节点由 LangGraph `StateGraph` 编排：

```text
retrieve_similar_issues
→ judge_duplicate
→ triage_issue（分类 / 优先级 / 风险 / 置信度）
→ 风险路由
   ├─ high → security_review → END
   └─ low / medium
      → draft_review
      → prepare_actions
      → END
```

分类结果为 `bug` / `feature` / `question` / `documentation` / `other`。具体 GitHub label 不由模型自由生成，而由仓库级 resolver 把 semantic category 映射到真实 label；已知语义分类在目标仓库没有验证映射时，Policy Gate 会 DEFER，而不是当成 NO_ACTION。

`draft_review` 使用 **category-aware completeness** 判断，而不是统一套 Bug 模板：

- Bug：只关注真正影响定位/复现的环境、版本、复现条件、预期/实际结果和关键日志；
- Feature：关注动机、目标、期望行为或验收标准，不要求无关的 OS、版本、错误日志；
- Documentation / Question / Other：按各自实际诉求判断必要上下文。

核心字段是 `needs_clarification`。只有缺失信息已经实际阻碍维护者理解问题或采取下一步时才为 `true`；当它为 `false` 时，代码会确定性清空 `missing_repro_fields` 和 `missing_info_confidence`，避免把“更多信息可能有帮助”误当成“必须公开追问”。

`prepare_actions` 默认不会把内部 `suggested_reply` 自动发布。普通 Issue 可以只产生 category label；只有 `needs_clarification=true` 且存在真正阻塞处理的最小缺失字段时，才生成确定性模板的 `request_missing_information` 动作。

高风险分支不生成公开命令，由 Policy Gate fail-closed 交给人工处理。

## 选择性自动化（Selective Automation）

```mermaid
flowchart LR
    A["Issue"] --> AG["Agent"]
    AG --> PG["Policy Gate"]
    PG -->|"AUTO_EXECUTE"| OB["Outbox"]
    OB --> W["Command Worker"]
    W --> GH["GitHub"]
    PG -->|"DEFER"| EQ["Exception Queue"]
    EQ --> H["Human（最小任务）"]
    PG -->|"NO_ACTION"| D["Done"]
```

### 三档 Rollout Mode

| Mode | 语义 |
|---|---|
| `off` | 完全兼容旧 review-all 行为（紧急回滚用），所有外部动作走人工 |
| `shadow` | Policy Gate 正常计算 `would_auto_execute / would_defer / would_no_action` 并落库，但真实动作仍走人工，用于收集策略与人工结果的对照 |
| `enforce` | `AUTO_EXECUTE` → 自动写回；`DEFER` → Exception Queue；`NO_ACTION` → 结束。缺少冻结策略时 fail-closed |

### 授权模型

`github_commands.authorization_source` 区分两种合法授权：

- **policy**：`review_task_id IS NULL`，`status in (approved, failed)`，必须携带 `policy_version`——自动化路径，不创建假人工审核任务；
- **human**：`review_task_id` 非空且 review 已 approved——人工路径。

Command Worker 只执行持有合法授权的命令，否则跳过。

### 当前自动化边界（诚实声明）

- 正式默认 Compose 配置仍为 `AUTOMATION_MODE=shadow`，Worker 的 GitHub 写开关默认 `GITHUB_WRITE_ENABLED=false`；默认启动不会进行 policy 无人写回；
- 自动标签 calibration 已完成，冻结 artifact 为 `eval/automation/policy.label.frozen.json`，当前只有 `add_category_label` 为 `enabled=true + allow_auto=true`，threshold **0.92**；
- unseen TEST（506 条）中共有 198 条进入自动标签集合：precision **93.94%**、coverage **39.13%**；这是 label auto-action 指标，不是完整 Agent 准确率；
- `request_missing_information`、`post_technical_reply`、`duplicate_action` 在冻结策略中仍 disabled；
- 当前 Policy Gate 是 **Issue-level all-or-nothing**：它逐个检查该 Issue 的全部 proposed actions；任意一个动作未被策略允许、低于阈值或缺少要求的 evidence，整个 Issue 都 DEFER。只有全部动作通过时才 AUTO_EXECUTE；
- 当前**没有实现 per-action 部分执行**，这是现阶段明确采用的 **Issue-level all-or-nothing** 设计：只要同一 Issue 含有任一未获授权动作，就整单 DEFER；冻结评测报告在评测时点把缺少 per-action authorization 记录为 enforce limitation，README 保留这一历史事实，但不把 per-action 拆分执行写成当前既定设计目标；
- 目前 enforce 只在受控 E2E 中验证了“单个 Issue 仅包含已校准 label action”时的真实自动写回链路；
- duplicate 继续 DEFER：Retrieval Evaluation 只证明候选召回/排序能力，不足以授权自动认定或自动关闭重复 Issue；
- security-risk 永远 fail-closed，不生成自动公开 side effect。

## 人工接管与 GitHub 写回

### Exception Queue（/ui/）

后端托管一个静态接管界面 `/ui/`（原生 HTML/CSS/JS，非前端框架）：

- 每张卡片**首屏**展示：需要人工的原因、你只需要判断的最小 `human_task`、Agent 已完成的工作、证据，然后才是 Issue 原文与模型输出；
- 列表按 `reason_code` 徽标分组，支持按 reason_code 筛选；列出 `reason_code / human_task / risk / category / created_at`；
- 审核人必须填写，备注可选；批准/拒绝前有确认弹窗；
- 审核台默认锁定：需输入 `REVIEW_ADMIN_TOKEN` 解锁，Token 仅保存在当前浏览器页面会话（`sessionStorage`），不进入 URL；可随时点击“锁定审核台”清除；
- 页面启用 CSP（`default-src 'self'`）与 `no-referrer`；`401`（Token 无效）或 `503`（服务端未配置 Token）时自动重新锁定。

![IssueFlow Review Console overview showing the exception queue, Agent assessment, risk level and pending human review](docs/images/review-console-overview.png)

*Review Console — 本地合成演示。维护者先看到“为什么需要我”的接管说明，再检查 Issue 上下文、Agent 判断与待执行动作。*

### 审核鉴权

审核 API 使用**最小共享管理员 Token**（`X-Review-Admin-Token` 请求头，`secrets.compare_digest` 比较），不是 RBAC / OAuth / IAM。未配置时返回 `503`，Token 不匹配返回 `401`。

### 决策语义

```mermaid
stateDiagram-v2
    [*] --> proposed
    proposed --> approved : 审核批准
    proposed --> rejected : 审核拒绝
    approved --> executing : Worker 取到命令
    executing --> executed : GitHub API 成功
    executing --> failed : 本地或 GitHub 失败
    approved --> [*]
    rejected --> [*]
    executed --> [*]
    failed --> [*]
```

批准流程：`review_tasks` 置为 `approved`、`github_commands` 置为 `approved`、写 `review_commands` Outbox，然后 Worker 依次执行命令。拒绝流程把仍处于 `proposed` 的命令置为 `rejected`，不进入执行队列。重复决定同一个任务返回 `409 Conflict`；并发决定由 `SELECT ... FOR UPDATE` 串行化。

### 崩溃语义（诚实边界）

- 已完成的 GitHub 写操作不自动重放：连接中断可能无法确认评论是否已创建，系统对这种情况的 HTTP 失败按 `retry_safe=false` 处理，避免静默发布重复评论；
- 外部成功但本地状态更新前崩溃的 `executing` 命令保留 `executing`，需要人工对账，不自动重放；
- GitHub 请求在失败前最多重试有限次数，并对 403/429 限流做专门识别；
- 恢复入口：`POST /recovery/outbox/dispatch`、`POST /recovery/agent-runs/{id}/requeue`、`POST /recovery/github-commands/{id}/requeue`（命令重入队要求已批准、`failed` 且 `retry_safe=true`）。恢复不会绕过审核状态和命令白名单。

## 历史 Issue 检索

### 链路与数据

历史 Issue 进入 `historical_issues`（按 `(repo, issue_number)` 幂等更新，内容哈希不变时不重复生成向量或 Chunk）。文本表示使用确定性规范化：Unicode NFKC、统一换行、空正文写为 `[empty]`、固定 `Title`/`Body` 字段顺序，表示版本记为 `issue-title-body-v2`。

```text
GitHub Backfill / Issue Webhook
→ Pull Request 过滤
→ repo + issue_number 幂等更新
→ content_hash 判断内容变化
→ lexical / vector 检索
→ RRF 融合 Top-K
→ LLM 结构化重复判断（仅审核建议）
```

### Embedding

Embedding 在 Docker CPU 内本地运行，不把 Issue 内容发送到外部 Embedding 服务：

- Provider：`fastembed`，模型 `BAAI/bge-small-en-v1.5`，384 维；
- 模型缓存位于持久化 Volume `fastembed_cache`（`/var/cache/issueflow/fastembed`）；缓存完成后可设 `EMBEDDING_LOCAL_FILES_ONLY=true` 禁止联网加载；
- 启动探针会实际生成向量并校验维度，不一致时终止启动，不写入数据库；
- Provider 取值：`fastembed`（本地 CPU）、`disabled`（禁用向量检索）、`fake`（仅测试/合成评测）、其他值明确失败，不会静默切换云端。

### 长文本策略

`bge-small-en-v1.5` 的输入上限为 512 tokens，系统有两种表示：

- **head512**：规范化标题+正文直接截到 512 tokens，同时记录原始/实际输入 token 数与 `truncated` 标记，不静默丢信息；
- **chunked**：用模型附带的真实 tokenizer 分块，默认每段 384 tokens、重叠 64、单 Issue 最多 16 段，每段重复标题；长查询也用同样策略拆成多个 Query Chunk 分别检索，再按 Issue 聚合。内容哈希与配置版本键不变时不重新分块或生成向量。

检索输入**只包含标题和正文**。标签、评论、`/duplicate of #N` 文本、维护者事后标记都明确排除在检索输入之外，避免把事后信息泄漏给查询。

### 三种检索模式

| 模式 | 机制 |
|---|---|
| `lexical` | PostgreSQL `pg_trgm` 相似度 + 全文检索（`websearch_to_tsquery`） |
| `vector` | pgvector cosine 距离（`head512` 或 `chunked`），HNSW 索引 |
| `hybrid` | 两路结果做 Reciprocal Rank Fusion（`rrf_k=60`） |

Embedding 未配置或失败时，`hybrid` 自动降级为 `lexical`，并在响应中标记 `degraded=true` 与原因。当前**未启用** cross-encoder 重排器（`DUPLICATE_RERANKER_ENABLED=false`）。

查询 API：

```text
GET /historical-issues/search?repo=owner/repo&query=login%20timeout&mode=hybrid&top_k=5
```

![IssueFlow review detail showing retrieved similar issues, retrieval scores, proposed GitHub commands and the human-review boundary](docs/images/review-console-detail.png)

*Review detail — local synthetic demo. Retrieved historical Issues remain review evidence; the Agent only proposes constrained `add_label` / `post_comment` commands.*

## 检索评测

### 数据集与协议

正式评测是 **maintainer-derived positive duplicate-relation retrieval benchmark**（检索召回/排序评估），不是完整“查重分类准确率”。评估仓库为 `microsoft/vscode`、`nodejs/node`、`rust-lang/rust`；正样本仅来自维护者明确操作（effective duplicate 事件或维护者身份写出的 duplicate comment）。

- DEV 集 **74** 条查询，TEST 集 **90** 条查询，合计 **164** 条（两者完全隔离）；
- 真值按无向 **Duplicate Cluster** 组织，共 **147** 个 cluster；同一 Issue / 同一 Cluster 不跨 DEV/TEST；
- 候选语料：同仓库、不是查询自身、且 `candidate.github_created_at < query_created_at`；
- 快照记录候选语料共 **6485** 条已入库 Issue（vscode 2236 / node 2126 / rust 2123），见 `repository_snapshots.json`；
- 指标：Recall@1/5/10、MRR@10、nDCG@10、延迟 P50/P95，按仓库单独计算后取仓库 Macro；置信区间用固定种子的 Query 级 Bootstrap（2000 次）。

### 冻结流程

参数与主方法在 TEST 前由 DEV 冻结，TEST 只运行一次且不参与任何参数重选：

1. DEV 比较 Query Prefix（无前缀 vs `Represent this sentence for searching relevant passages: `）与 Chunk 聚合（`max_chunk_score` vs `mean_top2_chunk_score`），按 Macro nDCG@10 选择，tie-break 倾向无前缀 / `max_chunk_score`；
2. 冻结配置写入 `eval/reports/rag_frozen_config.json`；
3. 预声明 primary retrieval method 写入 `eval/reports/rag_primary_method.json`；
4. 在 held-out TEST 上运行一次，结果冻结于 `eval/reports/rag_test.json`。

### TEST 结果（macro，90 条查询）

| 方法 | Recall@1 | Recall@5 | Recall@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|
| lexical | 0.2197 | 0.3442 | 0.3811 | 0.3009 | 0.3147 |
| vector_head512 | 0.4924 | 0.6723 | 0.7234 | 0.6151 | **0.6353** |
| **vector_chunked（冻结主方法）** | 0.4816 | **0.6835** | 0.7231 | **0.6009** | **0.6269** |
| hybrid_head512_rrf | 0.3634 | 0.6129 | 0.7313 | 0.5072 | 0.5534 |
| hybrid_chunked_rrf | 0.3511 | 0.6408 | 0.7393 | 0.5123 | 0.5620 |

**frozen primary 是 `vector_chunked`**：Macro Recall@5=0.6835、MRR@10=0.6009、nDCG@10=0.6269。它是在 DEV 上按预声明规则选出的主方法，并在 TEST 前冻结。

TEST 上的最高 macro nDCG@10 point estimate 是 `vector_head512`（0.6353），但它**不是**冻结主方法，也没有被用来重新选择配置。冻结的 primary `vector_chunked` 为 0.6269，point-estimate gap = 0.0084。

两种方法各自的 Bootstrap 95% CI 高度重叠：`vector_head512` [0.5464, 0.7172]、`vector_chunked` [0.5406, 0.7123]。但这些都是各方法自身的**边际 CI**；项目未执行 paired-delta significance test，因此**不对 0.0084 的差异作统计显著性声明**，也不进一步推导 significant / not significant / noise。

HNSW 相对 Exact 的 Top-K 召回率与顺序一致率均为 1.0（当前语料规模下 HNSW 相对 Exact 无召回损失，延迟也未明显降低，如实保留）。

### 从结果中学到什么

1. **Chunk 不是普遍更优**：`vector_chunked` 在 DEV 胜出被冻结为主方法，但 TEST 上 nDCG@10（0.6269）低于 `vector_head512`（0.6353）。长文本分块对召回有帮助，但对整体排序并非稳定占优。
2. **召回与排序是两回事**：`vector_chunked` 的 Recall@5（0.6835）略高于 `head512`（0.6723），但 nDCG 反而更低——长尾召回提升没有自动转成更好的排序质量。
3. **RRF Hybrid 是一个“扩大覆盖、削弱顶部排序”的 trade-off**：冻结配置下，`hybrid_chunked_rrf` 的 macro Recall@10 为 0.7393，高于 `vector_chunked` 的 0.7231——RRF 把 lexical 分路的候选带进较深位置，扩大了候选覆盖；但其 macro nDCG@10 为 0.5620，低于 `vector_chunked` 的 0.6269，说明当前 RRF fusion 在扩大覆盖的同时削弱了顶部排序质量。
4. **鲁棒性 bug 值得修**：极长或含长 `-` 连串的查询曾让 `websearch_to_tsquery` 解析栈溢出（`tsquery stack too small`）。通过把 tsquery 输入截断到 2000 字符并折叠 `-` 连串修复，并补充了回归测试。

完整的协议、泄漏边界与长文本策略见 [文档与评估产物](#文档与评估产物)。

## 可观测性

观测基于 PostgreSQL 持久化，不引入 OpenTelemetry / Prometheus / Grafana：

- `GET /traces`：Agent 运行列表（状态、模型、Prompt 版本、耗时、Token、估算成本）；
- `GET /traces/{trace_id}`：单条运行的节点级 Trace（每节点输入/输出摘要、耗时、Token、错误）；
- `GET /metrics/agent-runs`：聚合指标（完成/失败数、结构化输出成功率、平均 Token 与估算成本、耗时 P50/P95）。

## 安全与可靠性

| 风险 | 当前措施 |
|---|---|
| 伪造 Webhook | HMAC-SHA256 验签 |
| Webhook 重放 | `X-GitHub-Delivery` 唯一约束 |
| 非目标事件触发 | `event_name` / `action` 白名单，PR 事件忽略 |
| Prompt Injection | 固定 System Prompt，Issue 文本只作为数据 |
| 模型自由输出 | Pydantic 结构化输出 + 命令类型白名单 |
| 未授权 GitHub 写入 | `policy` / `human` 双授权 + 命令白名单；Worker 默认 `GITHUB_WRITE_ENABLED=false` 作为外部写入 kill switch |
| 高风险内容公开 | 高风险分支不生成公开命令 |
| Token 权限过大 | Fine-grained PAT，仅授权指定仓库 Issues 写权限 |
| 重复审核 / 重复命令 | `SELECT FOR UPDATE` + 唯一 `idempotency_key` |
| 任务丢失 | Outbox + 指数退避（基础 5s）+ 恢复接口 |
| 外部写重复 | 崩溃窗口下的重复评论不自动重放，保留 `executing` 待人工对账 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` `/health/embedding` | 健康与 Embedding 探针 |
| `POST` | `/webhooks/github` | 接收真实 GitHub Webhook |
| `POST` | `/agent/analyze` | 直接调用 Agent 分析（调试用） |
| `GET` | `/events` | 查询最近的 Issue 事件 |
| `GET` | `/review-tasks?status=pending` | 查询审核任务（需审核 Token） |
| `POST` | `/review-tasks/{id}/approve` | 批准并派发命令（需审核 Token） |
| `POST` | `/review-tasks/{id}/reject` | 拒绝并取消待批准命令（需审核 Token） |
| `GET` | `/historical-issues/search` | 历史 Issue 检索 |
| `GET` | `/traces` `/traces/{trace_id}` | Agent 运行与节点级 Trace |
| `GET` | `/metrics/agent-runs` | 聚合运行指标 |
| `POST` | `/recovery/outbox/dispatch` 等 | 恢复入口 |
| `GET` | `/docs` | OpenAPI 文档 |

## 技术栈

| 类型 | 技术 |
|---|---|
| 语言 / 后端 | Python 3.12、FastAPI、Uvicorn |
| Agent 编排 | LangGraph（workflow 模式） |
| 模型调用 | OpenAI-compatible API（默认 DeepSeek） |
| 数据校验 | Pydantic |
| 数据库 | PostgreSQL + pgvector、pg_trgm / 全文检索、Psycopg |
| 迁移 | Alembic |
| 队列 | Redis 7、RQ |
| 检索 | FastEmbed、`BAAI/bge-small-en-v1.5`（384 维）、RRF |
| 外部集成 | GitHub Webhook、GitHub REST API |
| 容器化 | Docker、Docker Compose |
| 本地 Webhook 转发 | Smee |

## 项目结构

```text
issueflow-agent/
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt / requirements-dev.txt
│   ├── alembic.ini
│   ├── migrations/          # Alembic 迁移（versions/0001..0006）
│   └── app/
│       ├── main.py          # FastAPI 入口，挂载路由与 /ui/
│       ├── api/             # webhooks / issues / agent / reviews / rag / observability / recovery / evals / health
│       ├── agents/          # LangGraph 工作流与结构化 Schema
│       ├── rag/             # embedding / chunking / indexing / retrieval / repository / sync / schema
│       ├── services/        # events / outbox / reviews / github / traces / evals
│       ├── workers/         # RQ worker（agent / command / recovery / index）
│       ├── ui/              # Review Console（index.html / app.js / styles.css）
│       ├── core/            # 配置、审核鉴权
│       ├── db/              # 数据库连接
│       └── models/          # 领域模型
├── docs/                    # rag.md、reliability.md、评测方法、泄漏边界、长文本策略 等
├── eval/
│   ├── automation/          # 自动标签 DEV/TEST、冻结 policy、预测 artifact、最终报告
│   ├── datasets/            # Retrieval qrels（dev/test）、clusters、snapshots、示例数据
│   └── reports/             # Retrieval frozen config / primary method / TEST report
├── scripts/                 # 检索评测 / 语料索引 / 真值采集 等 CLI
├── database/init/           # 基础 Schema 初始化 SQL（initdb）
├── docker-compose.yml
├── .env.example
└── README.md
```

## 快速开始

### 1. 环境要求

Docker、Docker Compose、Git、一个 GitHub 测试仓库、GitHub PAT 与可用的大模型 API。

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 填写 `GITHUB_WEBHOOK_SECRET`、`LLM_API_KEY`、`GITHUB_TOKEN`，并按需设置 `REVIEW_ADMIN_TOKEN`。`.env` 已被 `.gitignore` 忽略。

Compose 默认 `AUTOMATION_MODE=shadow`，Worker 默认 `GITHUB_WRITE_ENABLED=false`。shadow 会计算并记录 Policy Gate 决策，但不会按 policy 路径无人写回；只有显式配置冻结 policy、切到 `enforce` 并开启 GitHub 写开关后，满足策略的动作才可能真实 AUTO_EXECUTE。

### 3. 启动服务

```bash
docker compose up -d --build
```

`docker-compose.yml` 中的 `migrate` 服务会先执行 `alembic upgrade head` 完成迁移，之后后端与 Worker 才启动。启动后可通过 `/health` 与 `/health/embedding` 探针确认 Embedding 正常（不一致时后端拒绝启动）。

### 4. 健康检查

```bash
curl --noproxy '*' http://127.0.0.1:8000/health
```

### 5. 查看 Worker 日志

```bash
docker compose logs -f worker
```

## 配置 GitHub Webhook

本地开发用 Smee 把 GitHub 事件转发到本地后端：

```bash
npm install -g smee-client
smee -u https://smee.io/<YOUR_CHANNEL_ID> -t http://127.0.0.1:8000/webhooks/github
```

在测试仓库 `Settings → Webhooks → Add webhook` 中填写 Smee 的 Payload URL、`Content type: application/json`、`Secret`（与 `GITHUB_WEBHOOK_SECRET` 相同），Events 只勾选 `Issues`。

## 使用方式

### 查询待审核任务

```bash
curl --noproxy '*' \
  'http://127.0.0.1:8000/review-tasks?status=pending' \
  -H 'X-Review-Admin-Token: <REVIEW_ADMIN_TOKEN>'
```

### 批准任务

```bash
curl --noproxy '*' \
  -X POST 'http://127.0.0.1:8000/review-tasks/<REVIEW_TASK_ID>/approve' \
  -H 'Content-Type: application/json' \
  -H 'X-Review-Admin-Token: <REVIEW_ADMIN_TOKEN>' \
  -d '{"reviewer": "cy", "review_note": "确认 Agent 分析结果"}'
```

批准后命令进入 `review_commands` Outbox，由 Command Worker 执行并写回 GitHub。

### 拒绝任务

```bash
curl --noproxy '*' \
  -X POST 'http://127.0.0.1:8000/review-tasks/<REVIEW_TASK_ID>/reject' \
  -H 'Content-Type: application/json' \
  -H 'X-Review-Admin-Token: <REVIEW_ADMIN_TOKEN>' \
  -d '{"reviewer": "cy", "review_note": "分析结果不合适"}'
```

拒绝后仍处于 `proposed` 的命令被取消，不会进入执行队列。

### 历史 Issue 检索

```bash
curl --noproxy '*' \
  'http://127.0.0.1:8000/historical-issues/search?repo=owner/repo&query=login%20timeout&mode=hybrid&top_k=5'
```

## 验证路径

项目使用自身仓库进行真实 GitHub 写回验证，但严格区分 **链路验证** 与 **统计评测**。

### Human-authorized writeback

既有人工授权演示见
[GitHub Issue #5](https://github.com/chengyebi/issueflow-agent/issues/5)：

```text
Issue
→ Webhook
→ Agent
→ Review Task
→ Human approve
→ review_commands Outbox
→ Command Worker
→ GitHub write
→ executed
```

该演示用于验证人工审核后的真实 GitHub 写回链路。

![GitHub Issue 5 showing the bug label and approved follow-up comment written back by IssueFlow](docs/images/github-writeback-demo.png)

*真实人工授权写回演示。*

### Policy-authorized AUTO_EXECUTE E2E

随后又在
[GitHub Issue #8](https://github.com/chengyebi/issueflow-agent/issues/8)
验证了无人点击 Approve 的 policy 自动授权链路：

```text
feature / confidence=0.95
→ needs_clarification=false
→ proposed action: add_label(enhancement)
→ frozen label Policy Gate: AUTO_EXECUTE
→ authorization_source=policy
→ review_task_id=NULL
→ github_commands Outbox dispatched
→ Command Worker
→ executed
→ GitHub enhancement label
```

这次 E2E 使用了**仅测试期间生效、未提交到正式 repo-label resolver 的临时 `chengyebi/issueflow-agent -> enhancement` 映射**，并临时为测试 Worker 设置 `AUTOMATION_MODE=enforce` 与 `GITHUB_WRITE_ENABLED=true`。测试结束后正式 `.env` 恢复为 `AUTOMATION_MODE=shadow`、`GITHUB_WRITE_ENABLED=false`。

因此 #8 证明的是：

```text
Policy Gate
→ policy authorization
→ Transactional Outbox
→ RQ
→ Command Worker
→ GitHub API
```

这条执行链能够真实闭环；它**不是**冻结 TEST 数据集的一部分，也不表示 `chengyebi/issueflow-agent` 已加入正式校准 repo resolver，更不表示默认部署已经开启无人写入。

Retrieval 和 Label Automation 的可发布数字只来自各自冻结 DEV/TEST 协议，不用单次 E2E 冒充统计精度。

## 当前边界

- 审核鉴权是最小共享管理员 Token，不是 RBAC / OAuth / IAM，也不区分多用户角色；
- GitHub 外部写入无法提供跨系统 exactly-once；对无法确认是否成功的非幂等 side effect 不盲目重放，保留状态供人工对账；
- Label Automation benchmark 只覆盖 semantic triage + repo label resolver + risk gate + `add_category_label`，**不宣称**完整 Agent 准确率；
- `request_missing_information`、`post_technical_reply` 与 `duplicate_action` 尚未取得独立自动执行 calibration，因此当前冻结 policy 中保持 disabled；
- 当前自动化决策是 **Issue-level all-or-nothing**；任一 proposed action 不满足冻结 policy，整个 Issue 进入 DEFER，不进行 per-action 部分自动执行；
- 冻结评测报告在评测时点把缺少 per-action authorization 记录为 enforce limitation；当前生产代码仍选择 Issue-level all-or-nothing，因此 enforce 的适用范围必须按这一边界解释，不能宣称为“任意多动作 Issue 都可部分自动执行”；
- Retrieval Evaluation 衡量候选召回与排序，不等于 duplicate classification Precision / F1，因此 duplicate 自动执行未开启；
- 未启用 cross-encoder reranker；Issue Embedding 在本地 CPU 运行，不向外部 Embedding 服务发送 Issue 内容；
- 观测体系基于 PostgreSQL 持久化，未接入 OpenTelemetry / Prometheus / Grafana；
- 当前是 LangGraph workflow 型 Agent，不是多智能体系统，也没有 MCP / Kubernetes / Kafka 等当前规模不需要的设施；
- 默认 rollout 仍为 `shadow`，Worker 的 GitHub 写开关默认关闭；当前真实 enforce 只用于受控 E2E 验证，不宣称已经全面开放无人写入；
- README 的自动化数字来自冻结离线协议，不虚构生产流量、QPS、uptime、SLA 或完整 Agent accuracy。

## Roadmap

- 为 `request_missing_information`、`post_technical_reply` 等新 intent 建立独立 Ground Truth / DEV / unseen TEST calibration，在达到预设可靠性门槛前保持 disabled；
- 扩大自动化覆盖时继续遵守 Issue-level all-or-nothing：只有同一 Issue 的全部 proposed actions 都被冻结策略授权时才 AUTO_EXECUTE；是否未来引入更细粒度执行模型需另行设计与评测，不作为当前默认架构假设；
- 继续提升 duplicate retrieval 的召回与顶部排序，并在获得独立 duplicate-decision benchmark 前坚持人工接管；
- 补全 GitHub 外部 side effect 崩溃窗口下的状态对账与人工恢复工具；
- 增加更易复现的一键 Smoke Test，覆盖 Webhook → Agent → Policy → Outbox → Worker 的关键链路；
- 对长文本 Chunk、Hybrid fusion 与潜在 reranker 使用预注册评测，避免根据 TEST 结果反向调参；
- 完善成本与运行观测，但不为当前规模引入没有实际收益的复杂基础设施。

## 文档与评估产物

- [历史 Issue 混合检索与查重](docs/rag.md)
- [查重检索评测方法](docs/rag-evaluation-methodology.md)
- [查重评测的数据泄漏边界](docs/rag-data-leakage.md)
- [长文本 Issue 检索策略](docs/rag-long-document-strategy.md)
- [任务投递与恢复](docs/reliability.md)
- [选择性自动化 V2](docs/selective-automation-v2.md)
- [自动标签最终评测报告](eval/automation/FINAL-LABEL-AUTOMATION-REPORT.md)
- 冻结自动标签策略：`eval/automation/policy.label.frozen.json`
- [检索评测报告与数据集说明](eval/README.md)
- 评估产物：`eval/reports/rag_baseline_dev.json`、`eval/reports/rag_dev.json`、`eval/reports/rag_frozen_config.json`、`eval/reports/rag_primary_method.json`、`eval/reports/rag_test.json`
- 数据集：`eval/datasets/duplicate_qrels_dev.jsonl`、`eval/datasets/duplicate_qrels_test.jsonl`、`eval/datasets/duplicate_clusters.json`、`eval/datasets/repository_snapshots.json`
