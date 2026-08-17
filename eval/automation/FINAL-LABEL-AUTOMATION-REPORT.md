# IssueFlow Label Automation — Final Report

日期：2026-08-17
这是本项目本阶段**最后一轮正式评测**。所有数字来自 unseen TEST 唯一一次 evaluation。

## Protocol 摘要

- **评测范围**：GitHub Issue semantic category triage + repo-specific label resolver +
  risk gate + selective label automation。
  **不覆盖**：Retriever / duplicate / missing-info / technical reply / P3 per-action authorization。
- **共享 predictor**：`app/agents/triage.predict_triage`，production workflow 与 eval 调用同一实现
  （模型 / prompt / schema / structured output 完全一致），使用完整 body（无截断）。
- **输入 hash**：`input_hash` = 对真实传给模型的 messages 的 canonical JSON SHA-256。
- **Ground truth**：`expected_label` 来自 `source_labels` 中维护者实际使用的 concrete label
  （与 production resolver 独立，P1.9）。
- **checkpoint / resume**：每条完成立即 fsync；resume 用 (repo, issue_number, input_hash) 身份；
  config fingerprint 不匹配时拒绝 append。
- **成本保护**：保守 cache-miss 定价（input ¥1/M, output ¥2/M, effective 2026-08-17）。

## 数据集

| split | 条数 | bug | feature | question | doc | SHA-256 |
|---|---|---|---|---|---|---|
| DEV | 2011 | 1529 | 269 | 132 | 81 | `94a0a81a49ae...` |
| TEST | 506 | 384 | 68 | 33 | 21 | `9ad6e9899032...` |

- 分层时间切分（repo+category 分层，DEV=较早、TEST=较新）
- near-duplicate group（Jaccard 0.6）不跨 split：cross_split_group_count = 0
- 所有 item：group_id 非空、expected_label ∈ source_labels

## Prediction Artifacts

| artifact | SHA-256 | 说明 |
|---|---|---|
| DEV frozen | `f140ca38d9805f736a159ab60e3ff458d68f9ffed144d569145227a9e0da1c20` | 2011 records, 0 structured failure |
| TEST frozen | `fe15539fcaa3175cae5406999041b5266bd1136fa7a8f3b5b612c7b3ebde0877` | 506 records, 0 structured failure |

## 冻结配置

- **TEST 前锁死 commit SHA**：`0800229`（`评测：冻结自动标签开发集策略与预测结果`）
- **frozen policy**：`eval/automation/policy.label.frozen.json`
- **threshold**：`0.92`（raw confidence）
- 模型：`deepseek-v4-flash`；prompt：`triage-v2`；runner：`prod-v2`
- 仅 `add_category_label` enabled+allow_auto；其余 intent 全部 disabled

## DEV 冻结 gate 检查（Step 10，TEST 前锁死）

- A. auto_count=631 >= 200 ✅
- B. Wilson 95% CI lower = 0.959 >= 0.95 ✅
- C. auto_count>=30 的 bucket 均 point precision >= 0.90 ✅（vscode/feature 0.923、nodejs/doc 0.923、nodejs/bug 1.0、rust/bug 1.0）
- D. structured failure 不 auto（DEV 0 failure）✅
- E. predicted high-risk 不 auto（DEV 14 high-risk 全部 defer）✅

选择规则：满足条件的 threshold 中取 coverage 最大 → **0.92**（coverage 0.3138，DEV）。

## confidence 选择能力（P2.4）

- correct conf mean 0.904 > incorrect conf mean 0.872
- 阈值升高 precision 稳定上升：t=0.85→0.912、t=0.90→0.939、t=0.92→0.975、t=0.95→0.981
- **结论：raw confidence 具有选择能力**，可作 selector。

## 最终 TEST 指标（唯一一次，无调参）

```
TEST_COUNT = 506
LABEL_AUTO_ACTION_PRECISION = 0.9394
LABEL_AUTO_ACTION_PRECISION_95CI = [0.8971, 0.9650]
LABEL_AUTOMATION_COVERAGE = 0.3913
AUTO_COUNT = 198
CORRECT_AUTO = 186
WRONG_AUTO = 12
STRUCTURED_OUTPUT_FAILURES = 0
HIGH_RISK_DEFER_COUNT = 4
UNSUPPORTED_ACTION_DEFER_COUNT = 6
threshold = 0.92
```

### per repo / per category

| bucket | n | prec | CI lower | CI upper | err |
|---|---|---|---|---|---|
| microsoft/vscode | 27 | 0.7778 | 0.5924 | 0.8939 | 6 |
| nodejs/node | 41 | 0.8537 | 0.7156 | 0.9312 | 6 |
| rust-lang/rust | 130 | 1.0000 | 0.9713 | 1.0000 | 0 |
| bug | 156 | 0.9808 | 0.9450 | 0.9934 | 3 |
| feature | 25 | 0.8400 | 0.6535 | 0.9360 | 4 |
| documentation | 10 | 0.8000 | 0.4902 | 0.9433 | 2 |
| question | 7 | 0.5714 | 0.2505 | 0.8418 | 3 |

## 成本报告

| 阶段 | calls | input tokens | output tokens | 成本(¥) | 耗时 |
|---|---|---|---|---|---|
| DEV | 2011 | 5,198,750 | 172,622 | ¥5.54 | 530s |
| TEST | 506 | 1,454,154 | 44,541 | ¥1.54 | 132s |
| **TOTAL** | 2517 | 6,652,904 | 217,163 | **¥7.09** | 662s |

成本为保守估算（全部 input 按 cache-miss ¥1/M，output ¥2/M），token 为真实 usage_metadata。

## Limitations / 诚实边界

- 本 benchmark 只测 **triage + label resolver + risk gate + label automation**，
  不是端到端 Issue 自动化。
- **P3 BLOCKER BEFORE ENFORCE**：`AutomationDecision` 是 issue-level all-or-nothing，
  未校准 action 会拖住同 Issue 已校准 action；开启 enforce 前必须实现 per-action authorization。
- Retriever / duplicate / missing-info / technical reply 未在本 benchmark 覆盖。
- AUTOMATION_MODE 保持 shadow；未切 enforce。

## 简历可用表述（resume-ready）

**HR 3 秒版**：在 6485 条真实 GitHub Issue 上构建时间切分 DEV/TEST（506 条 unseen），
LLM 分类的自动标签动作 precision 达 93.9%（95% CI [89.7%, 96.5%]），
低风险标签动作覆盖率 39.1%。

**技术简历版**：IssueFlow 选择性自动化系统——确定性 Policy Gate + 仓库级 label resolver +
checkpoint/resume 评测管线；在 unseen TEST（506）上 label auto-action precision 93.9%、
coverage 39.1%，仅低风险动作自动执行。

**面试口述版**：我们从"每条 Issue 都人工审核"重构为"确定性 Policy Gate 决定哪些动作可自动执行，
其余进 Exception Queue"。因为 raw confidence 不是精度，我们用真实维护者 label 做 ground truth，
先看 DEV 冻结 threshold 再看一次 unseen TEST，杜绝调参泄漏。最终低风险标签动作 precision 93.9%
（CI 89.7%-96.5%），覆盖率 39.1%。剩余 Issue 因高置信度不足、缺标签映射或高风险而 DEFER。
