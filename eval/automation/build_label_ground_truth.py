"""从 historical_issues 构建 category label ground truth 数据集（v3）。

P1.1 分层切分：
- 每个 (repo, category) bucket 内按时间排序，较早 80% -> DEV、较新 20% -> TEST；
- 合并所有 bucket，保证 DEV 与 TEST 都包含四个 category（若某 repo 不存在某 category 不人为制造）。
- 显式输出 per-repo-category 计数、DEV/TEST 每 category 计数、时间范围、双 SHA-256。

P1.2 near-duplicate grouping：
- 使用 normalized-title token Jaccard（阈值写入 manifest），非 semantic embedding；
- group id 写入 dataset item（P1.7 修复持久化）；同一 group 不允许横跨 DEV/TEST；
- 不使用 TEST 预测结果做 grouping。

P1.7 group_id 持久化：
- selected 通过列表推导真正重建为带 group_id 的副本，验证 JSONL 中 group_id 非空。

P1.9 ground truth 独立性：
- expected_label 必须来自 source_labels 中维护者实际使用的 concrete label，
  绝不来自 production RepoLabelResolver（避免自证循环）；
- concrete-label ambiguous（同 category 多个不同 label）样本排除并记录。
"""

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from psycopg.rows import dict_row

from app.core.config import get_settings
from app.db.connection import connect

from schema import REPO_GROUND_TRUTH_LABELS

from schema import DatasetManifest, ExcludedItem, GroundTruthItem

SCHEMA_VERSION = "1.0"
SPLIT_STRATEGY = "repo_category_stratified_time"
DATASET_VERSION = "v3"
NEAR_DUP_JACCARD_THRESHOLD = 0.6
NEAR_DUP_GROUPING = "title-token-jaccard"

# 生命周期标签：不代表 category，出现时该 Issue 不应作为 category ground truth。
_LIFECYCLE_LABELS = {
    "duplicate",
    "*duplicate",
    "invalid",
    "wontfix",
    "won't fix",
    "by-design",
    "by design",
    "needs-info",
    "needs more info",
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9一-鿿]+")


def _is_excluded_lifecycle_label(labels: list[str]) -> bool:
    return any(label.strip().lower() in _LIFECYCLE_LABELS for label in labels)


def _fetch_issues(cur, repo: str, limit: int) -> list[dict]:
    cur.execute(
        """
        SELECT repo, issue_number, title, body, labels, state, github_created_at
        FROM historical_issues
        WHERE repo = %s
        ORDER BY github_created_at
        LIMIT %s
        """,
        (repo, limit),
    )
    return list(cur.fetchall())


def _labels_of(row) -> list[str]:
    labels = row["labels"]
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except json.JSONDecodeError:
            return []
    if not isinstance(labels, list):
        return []
    return [str(item) for item in labels]


def _core_category_from_labels(repo: str, labels: list[str]) -> tuple[str | None, str | None]:
    """从历史维护者 labels 解析 ground truth（P1.9）。

    返回 (category, concrete_label)：
    - concrete_label 是 source_labels 中实际出现的、映射到该 category 的原始 label
      （保留真实 GitHub spelling，如 "C-bug"）。
    - 若同一 category 有多个不同的 concrete label（concrete-label ambiguity），
      返回 (None, None)，调用方记录 AMBIGUOUS_CONCRETE_LABEL 排除。
    - 绝不使用 production RepoLabelResolver（避免自证循环）。
    """
    repo_rules = REPO_GROUND_TRUTH_LABELS.get(repo, {})
    categories: dict[str, list[str]] = {}
    for label in labels:
        # 精确匹配真实 label 原文（保留大小写）。
        category = repo_rules.get(label)
        if category is None:
            continue
        categories.setdefault(category, []).append(label)

    if len(categories) != 1:
        return None, None
    category = next(iter(categories))
    concrete_labels = set(categories[category])
    if len(concrete_labels) != 1:
        return None, None  # AMBIGUOUS_CONCRETE_LABEL
    return category, next(iter(concrete_labels))


def _has_any_ground_truth_label(repo: str, labels: list[str]) -> bool:
    """labels 中是否存在任何能映射到 semantic category 的 ground truth label。"""
    repo_rules = REPO_GROUND_TRUTH_LABELS.get(repo, {})
    return any(label in repo_rules for label in labels)


def _title_tokens(title: str) -> set[str]:
    return set(_TOKEN_SPLIT.split(title.lower()))


def _title_jaccard(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta and not tb:
        return 1.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


def _group_near_duplicates(
    items: list[GroundTruthItem],
) -> list[tuple[int, list[GroundTruthItem]]]:
    """按 title token Jaccard 分组（P1.2）。

    返回 [(group_id, items)]，同一 group 不允许跨 split。
    """
    groups: list[list[GroundTruthItem]] = []
    for item in sorted(items, key=lambda x: (x.repo, x.github_created_at)):
        placed = False
        for group in groups:
            # 仅同 repo 内比较（跨 repo 的 near-duplicate 不同 label 体系）。
            if group[0].repo != item.repo:
                continue
            if any(
                _title_jaccard(group_item.title, item.title) >= NEAR_DUP_JACCARD_THRESHOLD
                for group_item in group
            ):
                group.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])
    return list(enumerate(groups))


def build_dataset(
    *,
    repos: list[str],
    limit_per_repo: int,
    split: str,
    seed: int,
    out_dir: Path,
) -> tuple[Path, str, dict]:
    """构建并保存数据集，返回 (manifest_path, dataset_hash, stats)。"""
    items: list[GroundTruthItem] = []
    exclusions: list[ExcludedItem] = []
    source_labels_unknown: int = 0

    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        for repo in repos:
            rows = _fetch_issues(cur, repo, limit_per_repo)
            for row in rows:
                labels = _labels_of(row)
                if _is_excluded_lifecycle_label(labels):
                    exclusions.append(
                        ExcludedItem(
                            repo=row["repo"],
                            issue_number=row["issue_number"],
                            reason="存在生命周期标签（duplicate/invalid/wontfix 等）",
                        )
                    )
                    continue
                # P1.9：ground truth 来自 source_labels 的真实 concrete label，
                # 与 production resolver 完全独立。
                category, concrete_label = _core_category_from_labels(repo, labels)
                if category is None and concrete_label is None:
                    # 区分：无核心分类 与 concrete-label ambiguous。
                    if _has_any_ground_truth_label(repo, labels):
                        exclusions.append(
                            ExcludedItem(
                                repo=row["repo"],
                                issue_number=row["issue_number"],
                                reason="AMBIGUOUS_CONCRETE_LABEL（同 category 多个具体 label）",
                            )
                        )
                    else:
                        exclusions.append(
                            ExcludedItem(
                                repo=row["repo"],
                                issue_number=row["issue_number"],
                                reason="labels 缺少唯一核心分类",
                            )
                        )
                    continue
                items.append(
                    GroundTruthItem(
                        repo=row["repo"],
                        issue_number=row["issue_number"],
                        title=row["title"] or "",
                        body=row["body"] or "",
                        category=category,
                        expected_label=concrete_label,
                        source_labels=labels,
                        state=row.get("state") or "open",
                        github_created_at=str(row["github_created_at"] or ""),
                    )
                )

    # near-duplicate 分组（P1.2）。
    groups = _group_near_duplicates(items)
    group_id_of: dict[tuple[str, int], int] = {}
    for gid, group in groups:
        for item in group:
            group_id_of[(item.repo, item.issue_number)] = gid

    # P1.1 分层切分：每个 (repo, category) bucket 内按时间排序，较早 80% -> DEV。
    by_bucket: dict[tuple[str, str], list[GroundTruthItem]] = {}
    for item in items:
        key = (item.repo, item.category)
        by_bucket.setdefault(key, []).append(item)
    for bucket in by_bucket.values():
        bucket.sort(key=lambda x: x.github_created_at)

    dev_items: list[GroundTruthItem] = []
    test_items: list[GroundTruthItem] = []
    for bucket in by_bucket.values():
        # 按 group 整体切分：累计到 80% 前加入 DEV。
        bucket.sort(key=lambda x: x.github_created_at)
        n_dev = max(1, int(len(bucket) * 0.8))
        # 组内不拆分：从前往后累加 group，直到累计数 >= n_dev。
        group_accum = {}
        for item in bucket:
            gid = group_id_of[(item.repo, item.issue_number)]
            group_accum.setdefault(gid, []).append(item)
        # 按 group 最早 created_at 排序，保持时间顺序。
        ordered_groups = sorted(
            group_accum.values(), key=lambda g: min(x.github_created_at for x in g)
        )
        dev_bucket = []
        test_bucket = []
        count = 0
        for group in ordered_groups:
            if count < n_dev:
                dev_bucket.extend(group)
                count += len(group)
            else:
                test_bucket.extend(group)
        dev_items.extend(dev_bucket)
        test_items.extend(test_bucket)

    # P1.7：真正重建 selected 列表，让 group_id 持久化到每个 item。
    selected = [
        item.model_copy(
            update={"group_id": group_id_of[(item.repo, item.issue_number)]}
        )
        for item in (dev_items if split == "dev" else test_items)
    ]
    # 验证：所有 item 的 group_id 非空。
    if any(item.group_id is None for item in selected):
        raise RuntimeError("dataset 中存在 group_id 为空的 item")

    # 计算 SHA-256。
    payload = json.dumps(
        [item.model_dump() for item in selected],
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    dataset_hash = hashlib.sha256(payload).hexdigest()

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"label_ground_truth_{split}_v3.jsonl"
    with manifest_path.open("w", encoding="utf-8") as f:
        for item in selected:
            f.write(json.dumps(item.model_dump(), ensure_ascii=False) + "\n")

    ambiguous_count = _count_exclusions(exclusions).get(
        "AMBIGUOUS_CONCRETE_LABEL（同 category 多个具体 label）", 0
    )
    stats = {
        "dataset_version": DATASET_VERSION,
        "split_strategy": SPLIT_STRATEGY,
        "near_dup_grouping": NEAR_DUP_GROUPING,
        "near_dup_jaccard_threshold": NEAR_DUP_JACCARD_THRESHOLD,
        "split": split,
        "item_count": len(selected),
        "group_id_persisted": all(item.group_id is not None for item in selected),
        "ambiguous_concrete_label_count": ambiguous_count,
        "per_repo_category_counts": _per_repo_category(selected),
        "per_category_counts": _per_category(selected),
        "created_at_min": min((x.github_created_at for x in selected), default=""),
        "created_at_max": max((x.github_created_at for x in selected), default=""),
        "near_dup_group_count": len(groups),
        "cross_split_group_count": _cross_split_group_count(dev_items, test_items, group_id_of),
        "dataset_hash": dataset_hash,
        "excluded_count": len(exclusions),
        "exclusion_reasons": _count_exclusions(exclusions),
    }

    meta_path = out_dir / f"label_ground_truth_{split}_v3.manifest.json"
    meta_path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return manifest_path, dataset_hash, stats


def _per_repo_category(items: list[GroundTruthItem]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for item in items:
        rc = result.setdefault(item.repo, {})
        rc[item.category] = rc.get(item.category, 0) + 1
    return result


def _per_category(items: list[GroundTruthItem]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        result[item.category] = result.get(item.category, 0) + 1
    return result


def _cross_split_group_count(
    dev: list[GroundTruthItem],
    test: list[GroundTruthItem],
    group_id_of: dict[tuple[str, int], int],
) -> int:
    dev_groups = {group_id_of[(x.repo, x.issue_number)] for x in dev}
    test_groups = {group_id_of[(x.repo, x.issue_number)] for x in test}
    return len(dev_groups & test_groups)


def _count_exclusions(exclusions: list[ExcludedItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in exclusions:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 label ground truth 数据集 v3")
    parser.add_argument("--repos", default=get_settings().eval_repos)
    parser.add_argument("--limit-per-repo", type=int, default=5000)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", type=Path, default=Path("eval/automation/datasets"))
    args = parser.parse_args()

    repos = [item.strip() for item in args.repos.split(",") if item.strip()]
    path, dataset_hash, stats = build_dataset(
        repos=repos,
        limit_per_repo=args.limit_per_repo,
        split=args.split,
        seed=args.seed,
        out_dir=args.out_dir,
    )
    print(f"数据集写入: {path}")
    print(f"SHA-256: {dataset_hash}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
