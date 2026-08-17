# Label Automation Dataset

## Corpus

数据来自 `historical_issues` 中三个公开 GitHub 仓库的历史 Issue 快照：

| repo | total issues |
|---|---:|
| microsoft/vscode | 2236 |
| nodejs/node | 2126 |
| rust-lang/rust | 2123 |
| **Total** | **6485** |

三个仓库使用不同的 concrete label 体系，例如 VS Code 的 `bug` / `feature-request`、
Node.js 的 `confirmed-bug` / `feature request` / `doc`，以及 Rust 的 `C-bug`。
Ground Truth 构建先将维护者真实 concrete label 归一到 semantic category，但保留原始
`source_labels` 供独立验证。

## Ground Truth

仅保留具有唯一核心分类的 Issue，并排除生命周期标签与互相冲突的核心标签：

| category | clean records |
|---|---:|
| bug | 1913 |
| feature | 337 |
| question | 165 |
| documentation | 102 |
| **Total** | **2517** |

`expected_label` 必须直接存在于该 Issue 的 `source_labels` 中；production
`RepoLabelResolver` 不参与 Ground Truth 生成。

## DEV / TEST split

使用 `repo_category_stratified_time`：

- 每个 `(repo, category)` bucket 内按时间排序；
- 较早 80% 进入 DEV，较新 20% 进入 TEST；
- near-duplicate title group 不跨 split；
- TEST 在 DEV 阈值冻结前不用于参数选择。

| split | records | bug | feature | question | documentation |
|---|---:|---:|---:|---:|---:|
| DEV | 2011 | 1529 | 269 | 132 | 81 |
| TEST | 506 | 384 | 68 | 33 | 21 |

Dataset SHA-256：

- DEV: `94a0a81a49ae...`
- TEST: `9ad6e9899032...`

## Leakage controls

- 2415 个 near-duplicate groups，`cross_split_group_count = 0`
- 每个 item 的 `group_id` 均非空
- DEV / TEST group 集合无交集
- Ground Truth concrete label 与 production resolver 独立
- TEST 不参与 threshold、prompt 或 policy rule 的重新选择

当前正式数据文件：

- `datasets/label_ground_truth_dev_v3.jsonl`
- `datasets/label_ground_truth_dev_v3.manifest.json`
- `datasets/label_ground_truth_test_v3.jsonl`
- `datasets/label_ground_truth_test_v3.manifest.json`

历史 v1/v2 构建产物不属于当前正式协议，已从当前工作树移除；需要追溯时可通过 Git 历史查看。
