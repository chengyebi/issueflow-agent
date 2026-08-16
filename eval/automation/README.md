# Automation Evaluation

选择性自动化（IssueFlow V2）的离线评测设施。

## 文件

| 文件 | 用途 |
|---|---|
| `schema.py` | 数据集与评测报告 schema |
| `build_label_ground_truth.py` | 从 historical_issues 的 maintainer labels 构建 category ground truth |
| `run_automation_eval.py` | 运行确定性评测，计算覆盖率/精度/defer 分布 |
| `select_policy_thresholds.py` | 在 DEV 上扫描阈值、选择并冻结 policy artifact |
| `policy.json` | **初始冻结策略**：所有 intent 均 disabled（未校准，不伪造指标） |

## 方法与约束

- **Ground truth**：只用维护者明确的核心分类标签（bug / enhancement / question / documentation）。
  同一 Issue 有多个互相冲突的核心标签时排除并记录 exclusion reason。
- **可复现**：数据集保存 SHA-256，repo-aware，按 seed 稳定打乱。
- **防泄漏**：建立新的 DEV / unseen TEST 划分；TEST 在阈值冻结前不得查看。
- **不烧付费 API**：默认 runner 是确定性启发式分类器，不调用外部 LLM。
  若未来接入真实 LLM 评测，必须显式 `--allow-external` 并先获用户确认。
- **绝不伪造指标**：没有真实 calibration 数据的 intent 一律 `enabled=false`。

## 用法

```bash
# 1. 从数据库构建 DEV 数据集
python build_label_ground_truth.py --split dev --out-dir eval/automation

# 2. 在 DEV 上运行评测（确定性 runner）
python run_automation_eval.py \
  --dataset eval/automation/label_ground_truth_dev.jsonl \
  --policy eval/automation/policy.json \
  --out eval/reports/automation_eval.json

# 3. 扫描阈值并冻结（只读 DEV）
python select_policy_thresholds.py \
  --dataset eval/automation/label_ground_truth_dev.jsonl \
  --out eval/automation/policy.frozen.json

# 4. 仅当冻结完成、人工复核后可解锁 TEST（一次性观察）
python run_automation_eval.py \
  --dataset eval/automation/label_ground_truth_test.jsonl \
  --policy eval/automation/policy.frozen.json \
  --out eval/reports/automation_eval_test.json
```

## 核心指标

- `eligible_count` 可评估样本数
- `auto_execute_count` / `defer_count` / `no_action_count`
- `automation_coverage` = auto_execute / eligible
- `human_touch_rate` = defer / eligible
- `auto_action_precision` + Wilson 置信区间
- `error_auto_execute_count`（错误自动执行数）
- 按 intent / repo / confidence bucket 的 precision 与 coverage
- `defer_reason_distribution`

## 当前状态（诚实声明）

- **工具链已完成**，`policy.json` 是初始全 disabled 的冻结 artifact。
- **calibration 尚未执行**：`policy.frozen.json` 需要先有可用的
  historical_issues 数据卷和 ground truth，并人工复核启发式分类的质量后
  才能作为 production enforce 依据。
- 当前生产默认 `AUTOMATION_MODE=shadow`，即使有冻结 policy 也不会自动写回，
  直到 calibration 被人工确认。
