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
# 1. 从数据库构建 DEV 数据集（时间切分，排除近重复跨 split）
python build_label_ground_truth.py --split dev --state both --out-dir eval/automation

# 2. Stage 1：生成预测 artifact（启发式 smoke；不读取 ground truth）
python generate_predictions.py \
  --dataset eval/automation/label_ground_truth_dev.jsonl \
  --out eval/automation/predictions/predictions_dev.jsonl

# 3. Stage 2：在冻结 prediction 上运行评测（全 disabled policy 时 coverage=0）
python run_automation_eval.py \
  --predictions eval/automation/predictions/predictions_dev.jsonl \
  --policy eval/automation/policy.json \
  --out eval/reports/automation_eval.json

# 4. Stage 2：扫描阈值并生成冻结候选（只读 DEV，heuristic_smoke 时 enforce_ready=False）
python select_policy_thresholds.py \
  --predictions eval/automation/predictions/predictions_dev.jsonl \
  --dataset-manifest eval/automation/label_ground_truth_dev.manifest.json \
  --out eval/automation/policy.frozen.json

# 5. 仅当冻结完成、人工复核后可解锁 TEST（一次性观察）
python run_automation_eval.py \
  --predictions eval/automation/predictions/predictions_test.jsonl \
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

- **工具链已完成**：两阶段架构、时间切分、近重复防泄漏、阈值曲线、enforce 校验均已实现。
- **calibration 尚未执行**：`policy.json` 是初始全 disabled 的冻结 artifact；
  `policy.frozen.json` 需要真实 historical_issues 数据卷 + production-compatible prediction
  并人工复核后才能作为 production enforce 依据。
- 当前生产默认 `AUTOMATION_MODE=shadow`，即使有冻结 policy 也不会自动写回，
  直到 calibration 被人工确认。
