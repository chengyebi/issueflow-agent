# Label Automation Evaluation

本目录保存 IssueFlow 自动标签能力的正式评测协议、冻结策略与可复现实验产物。

## 评测目标

评测范围限定为：

```text
Issue title/body
→ production-compatible triage predictor
→ repo-specific label resolver
→ risk gate
→ deterministic Policy Gate
→ add_category_label
```

该 benchmark **不测量完整端到端 Issue 自动化**。`duplicate_action`、
`request_missing_information` 与 `post_technical_reply` 未在本轮取得独立自动执行校准，
因此不由该 benchmark 授权。

## 数据集

正式 Ground Truth 来自三个公开 GitHub 仓库的维护者真实标签：

- `microsoft/vscode`
- `nodejs/node`
- `rust-lang/rust`

清洗后共 2517 条唯一核心分类样本，按 repo + category 分层并在各 bucket 内做时间切分：

| split | records | bug | feature | question | documentation |
|---|---:|---:|---:|---:|---:|
| DEV | 2011 | 1529 | 269 | 132 | 81 |
| TEST | 506 | 384 | 68 | 33 | 21 |

近重复标题按 Jaccard 0.6 分组，group 不跨 DEV/TEST。`expected_label` 直接来自维护者
真实 concrete label，与 production repo-label resolver 独立，避免自证循环。

详细数据构建与防泄漏约束见 [DATASET.md](DATASET.md)。

## 两阶段评测

Prediction 与 Policy Evaluation 分离：

```text
Stage 1
dataset
→ app.agents.triage.predict_triage
→ frozen prediction artifact

Stage 2
frozen predictions
→ threshold scan / Policy Gate
→ precision / coverage / CI
```

Stage 2 不重新调用模型；所有 threshold 在同一份 prediction artifact 上计算，因此不会把
模型非确定性混入阈值比较。

## 冻结产物

- DEV predictions: `predictions/predictions_prod_dev_v3.jsonl`
- TEST predictions: `predictions/predictions_prod_test_v3.jsonl`
- frozen policy: `policy.label.frozen.json`
- final report: `FINAL-LABEL-AUTOMATION-REPORT.md`

正式模型为 `deepseek-v4-flash`，prompt 为 `triage-v2`。DEV 上冻结 threshold=0.92 后，
TEST 只观察一次，不使用 TEST 重新选阈值。

策略文件分为两类：

- `policy.json`：安全基线，所有 intent disabled；未显式选择已校准策略时保持 fail-closed
- `policy.label.frozen.json`：当前正式校准策略，仅授权 `add_category_label`

`policy.label.frozen.json` 的 threshold 为 0.92，其余 intent 保持 disabled。

## 最终 TEST 结果

| metric | value |
|---|---:|
| TEST records | 506 |
| auto actions | 198 |
| correct auto actions | 186 |
| precision | 93.94% |
| Wilson 95% CI | [89.71%, 96.50%] |
| coverage | 39.13% |
| structured-output failures | 0 |
| high-risk deferred | 4 |
| unsupported-action deferred | 6 |

这些数字是 **label auto-action** 指标，不代表完整 Agent accuracy，也不代表 duplicate
classification precision。

## 复现入口

主要脚本：

- `build_label_ground_truth.py`：从 historical issues 构建标签 Ground Truth
- `run_production_prediction.py`：调用 production predictor 生成冻结 prediction
- `select_policy_thresholds.py`：只读 DEV prediction 扫描阈值
- `evaluate_frozen_policy.py`：在冻结 policy 上评测
- `calibration_report.py`：生成 precision / coverage 与分桶报告

完整指标、artifact hash、成本与分桶结果见
[FINAL-LABEL-AUTOMATION-REPORT.md](FINAL-LABEL-AUTOMATION-REPORT.md)。
