# P1 真实 historical_issues 数据统计报告

日期：2026-08-17（只读连接用户 `issueflow` 数据卷，无任何写操作）

## 数据规模

| repo | total | open | closed |
|---|---|---|---|
| microsoft/vscode | 2236 | 562 | 1674 |
| nodejs/node | 2126 | 32 | 2094 |
| rust-lang/rust | 2123 | 590 | 1533 |
| **合计** | **6485** | **1184** | **5301** |

## 标签体系（真实，非假设）

| repo | 核心标签（>=50 次） | 说明 |
|---|---|---|
| microsoft/vscode | `bug`(451)、`feature-request`(182) | |
| nodejs/node | `question`(198)、`feature request`(189)、`confirmed-bug`(127)、`doc`(126) | |
| rust-lang/rust | `C-bug`(1361) | 分类标签体系完全不同 |

**关键发现**：初始 `CORE_LABEL_MAP` 只认 `bug/enhancement/question/documentation`，导致真实 ground truth 仅 649 条。扩展映射（`C-bug`、`feature-request`、`feature request`、`confirmed-bug`、`doc`）后扩展到 **2610 条核心样本**。

## Ground truth 可用性

扩展映射后的核心标签（排除生命周期标签 duplicate/*duplicate/invalid/wontfix，排除核心冲突）：

| category | 干净唯一分类样本 |
|---|---|
| bug | 1913 |
| feature | 337 |
| question | 165 |
| documentation | 102 |
| **合计** | **2517** |

- 核心+生命周期冲突：69 条（已排除）
- 核心分类互斥冲突（q+doc 等）：7 条（已排除）
- 无核心标签：2745 条（不进入 ground truth）
- 核心样本 state 分布：closed 540+（稳定历史），open 109 起始——**因此选择 state='both'**，
  closed issue 的维护者最终 label 是更稳定的 ground truth 来源（用户 P1 判断正确）。

## 数据集构建（v2：repo+category 分层 + 时间切分，防泄漏）

**v1 已废弃**（旧 TEST 只有 bug+feature，无法验证四分类系统），移至 `datasets/deprecated/`。
**v2** 使用 `repo_category_stratified_time`：每个 (repo, category) bucket 内按时间切分，
较早 80% -> DEV、较新 20% -> TEST。

| split | 条数 | bug | feature | question | doc | SHA-256 |
|---|---|---|---|---|---|---|
| **DEV** | 2011 | 1529 | 269 | 132 | 81 | `4781c8c7...` |
| **TEST** | 506 | 384 | 68 | 33 | 21 | `138d6471...` |

- **四类都进入 DEV 与 TEST** ✅（修复 v1 的 TEST 无 question/doc 问题）
- per-repo-category 明细：
  - vscode: DEV feature 143/bug 343；TEST feature 36/bug 86
  - nodejs: DEV bug 98/question 132/feature 126/doc 81；TEST bug 25/question 33/feature 32/doc 21
  - rust: DEV bug 1088；TEST bug 273
- near-duplicate grouping：`title-token-jaccard`（阈值 0.6，写入 manifest），2415 groups，**cross_split_group_count = 0** ✅
- DEV 时间范围 2015-05 → 2026-03；TEST 2019-01 → 2026-08
- **TEST 内容/指标冻结前未查看**
- 排除统计：无核心标签 3593、生命周期 351、核心冲突 24（共 3968）

## Production-compatible prediction 成本估算

当前只生成了 heuristic_smoke 预测（免费，raw_confidence 固定 1.0）。

若要运行与生产一致的 LLM prediction（DEV 2013 条，每条一次 triage 调用）：

- 平均输入 ≈ 1495 chars/条（含 title+body）→ 全量 ≈ 3.0M chars ≈ **0.75M input tokens**
- 每条输出 ~100 tokens → **0.2M output tokens**
- 按 deepseek-v4-flash 估算：输入 ~$0.20 + 输出 ~$0.22 ≈ **$0.42**
- 若用 pro 级模型：≈ $3-5

**决策**：成本很小，但本轮**未执行付费 LLM**（遵循"先报告成本，不擅自烧额度"约束；
且 production 分类器应与真实 workflow LLM 一致，需用户确认模型后执行）。

## 当前 precision-coverage（heuristic_smoke，不可用于 production）

| threshold | auto_count | coverage | precision | CI lower | CI upper |
|---|---|---|---|---|---|
| 0.0–0.99 | 1726 | 0.857 | 0.838 | 0.820 | 0.854 |

所有阈值结果相同：heuristic_smoke 的 confidence 固定 1.0，阈值无区分度。
**该曲线不代表 production 能力**，只证明工具链工作正常。

## 距 >=80% coverage 的真实差距

- heuristic_smoke 显示 85.7% coverage / 83.8% precision（CI 下限 82%），
  但这是**关键词启发式**，未与生产 LLM 对齐，不能作为产品指标。
- 需要：production-compatible LLM prediction → 真实 precision-coverage curve →
  人工确认 precision 达标 → 才可 freeze 正式 policy。
- **当前 product 目标（>=80% coverage 且 precision 达标）尚未达成**，
  因为 production calibration 尚未运行。
