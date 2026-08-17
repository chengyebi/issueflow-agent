# IssueFlow Label Automation — Final Evaluation

Evaluation date: 2026-08-17

本报告记录自动标签能力的冻结 DEV/TEST 评测。所有 TEST 数字来自阈值冻结后的唯一一次
held-out evaluation。

## Scope

评测链路：

```text
semantic triage
→ repo-specific label resolver
→ risk gate
→ deterministic Policy Gate
→ add_category_label
```

不覆盖 Retrieval / duplicate decision / missing-information comment /
technical reply，因此结果不能解释为完整端到端 Issue 自动化准确率。

Production workflow 与 eval 共用 `app.agents.triage.predict_triage`，模型、prompt、schema
与 structured output 路径保持一致。

## Dataset

| split | records | bug | feature | question | documentation |
|---|---:|---:|---:|---:|---:|
| DEV | 2011 | 1529 | 269 | 132 | 81 |
| TEST | 506 | 384 | 68 | 33 | 21 |

- repo + category 分层时间切分
- near-duplicate group 不跨 split
- `expected_label` 来自维护者真实 `source_labels`
- Ground Truth 与 production resolver 独立

详见 [DATASET.md](DATASET.md)。

## Frozen prediction artifacts

| artifact | SHA-256 | records |
|---|---|---:|
| DEV | `f140ca38d9805f736a159ab60e3ff458d68f9ffed144d569145227a9e0da1c20` | 2011 |
| TEST | `fe15539fcaa3175cae5406999041b5266bd1136fa7a8f3b5b612c7b3ebde0877` | 506 |

两份 production prediction 的 structured-output failure 均为 0。

## Reproducibility

Production-compatible prediction runner 保留以下可复现约束：

- `input_hash` 是对真正传给模型的 messages 做 canonical JSON 后计算的 SHA-256，用于标识模型实际看到的输入；
- 每条完成记录立即 append、flush 并 `fsync`，避免长任务中断时丢失已完成结果；
- `--resume` 以 `(repo, issue_number, input_hash)` 识别已完成样本；
- resume 时若 config fingerprint 不一致则拒绝继续写入原 artifact；fingerprint 包含 runner/model、prompt、schema、repo-label resolver、Git SHA 与定价日期等配置身份。

这些机制只用于保证评测 artifact 的可追溯性与断点续跑一致性，不改变冻结 TEST 的统计结果。

## Frozen policy

- TEST 前冻结 commit: `0800229`
- policy: `eval/automation/policy.label.frozen.json`
- threshold: **0.92**
- model: `deepseek-v4-flash`
- prompt: `triage-v2`
- runner: `prod-v2`
- only `add_category_label` is enabled + allow_auto

DEV gate 在 threshold=0.92 时：

- auto_count = 631
- precision = 0.9746
- Wilson 95% CI lower = 0.959
- coverage = 0.3138
- predicted high-risk items are all deferred

阈值选择规则是在满足 TEST 观察前已冻结的 precision / sample-size gate 的候选中最大化 coverage。

## Confidence selection behavior

DEV 上 raw confidence 对正确/错误 prediction 有区分能力：

- correct mean confidence: 0.904
- incorrect mean confidence: 0.872
- precision: 0.912 @ 0.85
- precision: 0.939 @ 0.90
- precision: 0.975 @ 0.92
- precision: 0.981 @ 0.95

因此本轮实验允许用 raw confidence 作为自动标签 selector；该结论只适用于本次模型、
prompt 与数据分布。

## Held-out TEST result

```text
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

### Per repository / category

| bucket | n | precision | CI lower | CI upper | errors |
|---|---:|---:|---:|---:|---:|
| microsoft/vscode | 27 | 0.7778 | 0.5924 | 0.8939 | 6 |
| nodejs/node | 41 | 0.8537 | 0.7156 | 0.9312 | 6 |
| rust-lang/rust | 130 | 1.0000 | 0.9713 | 1.0000 | 0 |
| bug | 156 | 0.9808 | 0.9450 | 0.9934 | 3 |
| feature | 25 | 0.8400 | 0.6535 | 0.9360 | 4 |
| documentation | 10 | 0.8000 | 0.4902 | 0.9433 | 2 |
| question | 7 | 0.5714 | 0.2505 | 0.8418 | 3 |

## Cost

| stage | calls | input tokens | output tokens | estimated cost | elapsed |
|---|---:|---:|---:|---:|---:|
| DEV | 2011 | 5,198,750 | 172,622 | ¥5.54 | 530s |
| TEST | 506 | 1,454,154 | 44,541 | ¥1.54 | 132s |
| **Total** | **2517** | **6,652,904** | **217,163** | **¥7.09** | **662s** |

Token 数来自模型 usage metadata；成本按当次评测使用的保守 cache-miss 单价估算。

## Limitations

- 本 benchmark 仅校准 `add_category_label`
- `request_missing_information`、`post_technical_reply`、`duplicate_action` 未由本 benchmark 授权
- 当前 production router 采用 **Issue-level all-or-nothing**：同一 Issue 只要存在任一未获
  冻结策略授权的 proposed action，整单 DEFER
- Retrieval benchmark 只评估候选召回与排序，不等于 duplicate classification precision
- production 默认 rollout 仍为 `shadow`
- `enforce` 仅在受控 E2E 中验证过单个已校准 label action 的真实写回链路
