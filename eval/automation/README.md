# Automation Evaluation

选择性自动化（IssueFlow V2）的离线评测设施。

## 两阶段架构（P0-3 修复）

评测严格分为两个阶段，**prediction 与 policy evaluation 完全分离**：

```
Stage 1: dataset
         -> production-compatible predictor (generate_predictions.py)
         -> predictions.jsonl    （冻结，只生成一次）

Stage 2: predictions.jsonl
         -> threshold scan / Policy Gate (run_automation_eval.py, select_policy_thresholds.py)
         -> precision / coverage curve
```

- **Stage 1 绝不允许读取 ground truth 作为预测输入**（P0-1）：
  每条记录区分 `true_category` / `predicted_category` / `prediction_confidence`；
  `true_category` 只存在于 artifact 中，供 Stage 2 比较。
- **Stage 2 不调用模型**：threshold scan 在完全相同的 prediction 上运行，
  不会因为模型非确定性导致每个 threshold 使用不同 prediction。
- **确定性启发式 runner（heuristic_smoke）**：`raw_confidence` 固定 1.0，
  `runner_type=heuristic_smoke`，只能用于验证评测框架，
  **不允许 freeze production policy**（`enforce_ready=False`）。
- **production-compatible prediction**：必须运行与生产一致的分类逻辑，
  保存 `predicted_category / raw_model_confidence / model_name / prompt_version /
  input_hash`；正式 threshold selection 只能读取它的 frozen artifact。

## 文件

| 文件 | 用途 |
|---|---|
| `schema.py` | 数据集与评测报告 schema |
| `build_label_ground_truth.py` | 从 historical_issues 的 maintainer labels 构建 category ground truth |
| `generate_predictions.py` | **Stage 1**：dataset -> predictions.jsonl（启发式 smoke / 未来 production LLM） |
| `run_automation_eval.py` | **Stage 2**：只读 predictions + policy，计算覆盖率/精度/defer 分布 |
| `select_policy_thresholds.py` | **Stage 2**：同一份 prediction 上扫描阈值，输出 precision-coverage curve + Wilson CI |
| `policy.json` | **初始冻结策略**：所有 intent 均 disabled（未校准，不伪造指标） |

## 方法与约束

- **Ground truth**：只用维护者明确的核心分类标签（bug / enhancement / question / documentation）。
  同一 Issue 有多个互相冲突的核心标签时排除并记录 exclusion reason；
  出现 duplicate/invalid/wontfix 等生命周期标签时排除。
- **可复现**：数据集保存 SHA-256，repo-aware，按 seed 稳定打乱。
- **防泄漏（P1）**：
  - 支持时间切分：DEV = 较早时间段、TEST = 较新时间段；
  - 标题近重复的 Issue 归入同一 group，不跨 split；
  - TEST 在阈值冻结前不得查看。
- **不烧付费 API**：默认 runner 是确定性启发式分类器，不调用外部 LLM。
  若未来接入真实 LLM 评测，必须显式 `--runner production_llm` 并先获用户确认
  成本（样本数 / 预计 calls / tokens / 美元成本）。
- **绝不伪造指标**：没有真实 calibration 数据的 intent 一律 `enabled=false`。

## 用法

```bash
# 1. 从数据库构建 DEV v2 数据集（repo+category 分层时间切分，Jaccard near-dup 防泄漏）
python build_label_ground_truth.py --split dev --repos 'microsoft/vscode,nodejs/node,rust-lang/rust' \
  --out-dir eval/automation/datasets

# 2. Stage 1：生成预测 artifact（启发式 smoke；不读取 ground truth）
python generate_predictions.py \
  --dataset eval/automation/datasets/label_ground_truth_dev_v2.jsonl \
  --out eval/automation/predictions/predictions_dev_v2.jsonl

# 3. Stage 2：在冻结 prediction 上运行评测（全 disabled policy 时 coverage=0）
python run_automation_eval.py \
  --predictions eval/automation/predictions/predictions_dev_v2.jsonl \
  --policy eval/automation/policy.json \
  --out eval/reports/automation_eval.json

# 4. Stage 2：扫描阈值并生成冻结候选（只读 DEV，heuristic_smoke 时 enforce_ready=False）
python select_policy_thresholds.py \
  --predictions eval/automation/predictions/predictions_dev_v2.jsonl \
  --dataset-manifest eval/automation/datasets/label_ground_truth_dev_v2.manifest.json \
  --out eval/automation/policy.frozen.json

# 5. 仅当冻结完成、人工复核后可解锁 TEST（一次性观察）
python run_automation_eval.py \
  --predictions eval/automation/predictions/predictions_test_v2.jsonl \
  --policy eval/automation/policy.frozen.json \
  --out eval/reports/automation_eval_test.json
```

## 核心指标

- `eligible_count` / `auto_execute_count` / `defer_count` / `no_action_count`
- `automation_coverage` = auto_execute / eligible
- `human_touch_rate` = defer / eligible
- `auto_action_precision` + **Wilson 置信区间**（不只 point estimate）
- `error_auto_execute_count`（错误自动执行数）
- 按 intent / repo / confidence bucket 的 precision 与 coverage
- `defer_reason_distribution`

## 正式 policy 冻结条件（P0-7）

enforce 模式加载策略时，`load_calibrated_policy(for_enforce=True)` 严格校验：

- `source_dataset_hash` 非空（来自 dataset manifest 的真实 SHA-256，不是 `""`）；
- 任一 enabled+allow_auto intent 必须有非空 `observed_precision`、`sample_count > 0`；
- `prediction_artifact_hash` 非空（预测 artifact 的 SHA-256）；
- 缺任一条件即 fail-closed，不允许 enforce 自动执行。

## 当前状态（诚实声明）

- **工具链已完成**：两阶段架构、repo+category 分层时间切分、Jaccard near-dup 防泄漏、
  仓库级 label resolver、阈值曲线、enforce 校验均已实现。
- **真实数据已接入**（P1.6）：从用户 `issueflow` 数据卷只读构建了 v2
  DEV 2011 条 + unseen TEST 506 条（见 `P1-DATA-REPORT.md`），
  四类都进入 DEV 与 TEST；near-dup 2415 groups 全部不跨 split；TEST 冻结前未查看。
- **v1 数据集已废弃**（旧 TEST 只有 bug+feature），移至 `datasets/deprecated/`。
- **production-compatible LLM prediction 未执行**：成本估算约 $0.42（DEV 全量），
  需用户确认模型后运行；正式 threshold selection 只读取 production prediction artifact。
- **仓库级 label resolver（P1.3）已实现**：Agent 只输出 category，具体 label 由
  `repo_labels.REPO_CATEGORY_LABELS` 决定；无验证映射的 (repo, category) 必须 DEFER。
- **REQUEST_MISSING_INFORMATION 已隔离（P1.5）**：独立 confidence、强制 disabled，
  不共享 category calibration。
- `policy.json` 仍是初始全 disabled 的冻结 artifact；正式 freeze 需 production prediction + 人工复核。
- 当前生产默认 `AUTOMATION_MODE=shadow`，即使有冻结 policy 也不会自动写回。
