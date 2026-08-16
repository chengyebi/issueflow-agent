"""从 historical_issues 构建 category label ground truth 数据集。

- 从数据库读取已有 maintainer labels。
- 只保留核心分类映射清晰的样本：bug/enhancement/question/documentation。
- 同一 Issue 有多个互相冲突的核心分类标签时排除并记录。
- 输出可复现的 JSONL，带 SHA-256。
- 默认构建 DEV；TEST 需要显式构建为 unseen holdout，且阈值冻结前不得查看。

切分（P1）：
- 默认按时间切分：DEV = 较早时间段，TEST = 较新时间段；
- 同一 repo 的 near-duplicate 可能横跨时间边界，因此提供 --exclude-near-duplicates
  选项，把 title 高度相似的 Issue 归入同一组并整体放入同一 split。
- state 支持 open / closed / both。closed 往往由维护者完成处理，label 更稳定，
  但必须排除 duplicate/invalid 等生命周期标签污染，见 _is_excluded_lifecycle_label。
"""

import argparse
import hashlib
import json
import random
from pathlib import Path

from psycopg.rows import dict_row

from app.core.config import get_settings
from app.db.connection import connect

from schema import CORE_LABEL_MAP, DatasetManifest, ExcludedItem, GroundTruthItem

SCHEMA_VERSION = "1.0"

# 生命周期标签：不代表 category，出现时该 Issue 不应作为 category ground truth。
# 注意：question / doc 是核心分类标签，绝不能作为生命周期标签排除。
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


def _is_excluded_lifecycle_label(labels: list[str]) -> bool:
    return any(label.strip().lower() in _LIFECYCLE_LABELS for label in labels)


def _fetch_issues(cur, repo: str, state: str, limit: int) -> list[dict]:
    if state == "both":
        cur.execute(
            """
            SELECT repo, issue_number, title, body, labels, state,
                   github_created_at
            FROM historical_issues
            WHERE repo = %s
            ORDER BY github_created_at
            LIMIT %s
            """,
            (repo, limit),
        )
    else:
        cur.execute(
            """
            SELECT repo, issue_number, title, body, labels, state,
                   github_created_at
            FROM historical_issues
            WHERE repo = %s AND state = %s
            ORDER BY github_created_at
            LIMIT %s
            """,
            (repo, state, limit),
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


def _core_category(labels: list[str]) -> tuple[str | None, list[str]]:
    """从 labels 推导单一核心分类。

    返回 (category, mapped_labels)。若冲突或缺失返回 (None, [])。
    """
    mapped = []
    for label in labels:
        norm = label.strip().lower()
        if norm in CORE_LABEL_MAP:
            mapped.append(CORE_LABEL_MAP[norm])
    if len(mapped) == 1:
        return mapped[0], mapped
    return None, mapped


def _title_near_duplicate_key(title: str) -> str:
    """把标题归一化为近重复 key（小写、去标点、去空格）。"""
    return "".join(ch for ch in title.lower() if ch.isalnum())


def _group_near_duplicates(
    items: list[GroundTruthItem],
) -> list[list[GroundTruthItem]]:
    """把标题高度相似的 Issue 归为一组，保证 group 不跨 split。"""
    groups: list[list[GroundTruthItem]] = []
    seen: dict[str, int] = {}
    for item in items:
        key = _title_near_duplicate_key(item.title)
        if key in seen:
            groups[seen[key]].append(item)
        else:
            seen[key] = len(groups)
            groups.append([item])
    return groups


def build_dataset(
    *,
    repos: list[str],
    limit_per_repo: int,
    split: str,
    seed: int,
    out_dir: Path,
    state: str = "open",
    time_split: bool = True,
    exclude_near_duplicates: bool = False,
) -> tuple[Path, str]:
    """构建并保存数据集，返回 (manifest_path, dataset_hash)。"""
    items: list[GroundTruthItem] = []
    exclusions: list[ExcludedItem] = []

    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        for repo in repos:
            rows = _fetch_issues(cur, repo, state, limit_per_repo)
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
                category, mapped = _core_category(labels)
                if category is None:
                    exclusions.append(
                        ExcludedItem(
                            repo=row["repo"],
                            issue_number=row["issue_number"],
                            reason=(
                                "labels 缺少唯一核心分类"
                                f"（{sorted(mapped)}）"
                            ),
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
                        source_labels=labels,
                        state=row.get("state") or "open",
                        github_created_at=str(row["github_created_at"] or ""),
                    )
                )

    items.sort(key=lambda item: (item.repo, item.issue_number, item.github_created_at))

    if time_split and items:
        # DEV = 较早 80%，TEST = 较新 20%（按 github_created_at）。
        n_dev = max(1, int(len(items) * 0.8))
        if split == "dev":
            selected = items[:n_dev]
        else:
            selected = items[n_dev:]
    else:
        # 可复现随机切分。
        rng = random.Random(seed)
        rng.shuffle(items)
        n_dev = max(1, int(len(items) * 0.8))
        selected = items[:n_dev] if split == "dev" else items[n_dev:]

    if exclude_near_duplicates:
        # 近重复 group 整体放入同一 split：按 group 最早 created_at 决定归属。
        groups = _group_near_duplicates(items)
        groups.sort(key=lambda g: g[0].github_created_at)
        split_groups: list[list[GroundTruthItem]] = []
        cursor = 0
        group_index = 0
        for group in groups:
            if cursor < n_dev:
                split_groups.append(group)
                cursor += len(group)
            else:
                split_groups.append(group)
            group_index += 1
        # 简化：DEV 取累计 <= n_dev 的 group，其余进 TEST。
        dev_items: list[GroundTruthItem] = []
        test_items: list[GroundTruthItem] = []
        accumulated = 0
        for group in groups:
            if accumulated < n_dev:
                dev_items.extend(group)
                accumulated += len(group)
            else:
                test_items.extend(group)
        selected = dev_items if split == "dev" else test_items

    # 计算 SHA-256（基于完整 items + split 划分，保证可复现）。
    payload = json.dumps(
        [item.model_dump() for item in selected],
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    dataset_hash = hashlib.sha256(payload).hexdigest()

    manifest = DatasetManifest(
        dataset_name=f"automation-label-{split}",
        split=split,
        dataset_hash=dataset_hash,
        source_repos=repos,
        item_count=len(selected),
        items=selected,
        exclusions=exclusions,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"label_ground_truth_{split}.jsonl"
    with manifest_path.open("w", encoding="utf-8") as f:
        for item in selected:
            f.write(json.dumps(item.model_dump(), ensure_ascii=False) + "\n")

    meta_path = out_dir / f"label_ground_truth_{split}.manifest.json"
    meta = manifest.model_dump(exclude={"items"})
    meta["item_count"] = len(selected)
    meta["excluded_count"] = len(exclusions)
    meta["exclusion_reasons"] = _count_exclusions(exclusions)
    meta["dataset_hash"] = dataset_hash
    meta["split_mode"] = "time" if time_split else "random"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return manifest_path, dataset_hash


def _count_exclusions(exclusions: list[ExcludedItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in exclusions:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 label ground truth 数据集")
    parser.add_argument("--repos", default=get_settings().eval_repos)
    parser.add_argument("--limit-per-repo", type=int, default=1000)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", type=Path, default=Path("eval/automation"))
    parser.add_argument(
        "--state", choices=["open", "closed", "both"], default="open",
        help="closed 由维护者完成处理、label 更稳定；both 包含所有状态",
    )
    parser.add_argument(
        "--time-split", action="store_true", default=True,
        help="DEV=较早、TEST=较新 的时间切分（默认开启）",
    )
    parser.add_argument(
        "--no-time-split", action="store_true", default=False,
        help="改用可复现随机切分",
    )
    parser.add_argument(
        "--exclude-near-duplicates", action="store_true", default=True,
        help="标题近重复的 Issue 归入同一 group，不跨 split",
    )
    args = parser.parse_args()

    repos = [item.strip() for item in args.repos.split(",") if item.strip()]
    time_split = not args.no_time_split
    path, dataset_hash = build_dataset(
        repos=repos,
        limit_per_repo=args.limit_per_repo,
        split=args.split,
        seed=args.seed,
        out_dir=args.out_dir,
        state=args.state,
        time_split=time_split,
        exclude_near_duplicates=args.exclude_near_duplicates,
    )
    print(f"数据集写入: {path}")
    print(f"SHA-256: {dataset_hash}")


if __name__ == "__main__":
    main()
