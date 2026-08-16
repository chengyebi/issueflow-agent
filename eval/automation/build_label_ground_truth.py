"""从 historical_issues 构建 category label ground truth 数据集。

- 从数据库读取已有 maintainer labels。
- 只保留核心分类映射清晰的样本：bug/enhancement/question/documentation。
- 同一 Issue 有多个互相冲突的核心分类标签时排除并记录。
- 输出可复现的 JSONL，带 SHA-256。
- 默认只构建 DEV；TEST 需要显式构建为 unseen holdout，且阈值冻结前不得查看。
"""

import argparse
import hashlib
import json
import os
import random
from pathlib import Path

from app.core.config import get_settings
from app.db.connection import connect

from schema import CORE_LABEL_MAP, DatasetManifest, ExcludedItem, GroundTruthItem

SCHEMA_VERSION = "1.0"


def _fetch_issues(cur, repo: str, limit: int) -> list[dict]:
    cur.execute(
        """
        SELECT repo, issue_number, title, body, labels, github_created_at
        FROM historical_issues
        WHERE repo = %s
          AND state = 'open'
        ORDER BY issue_number
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


def build_dataset(
    *,
    repos: list[str],
    limit_per_repo: int,
    split: str,
    seed: int,
    out_dir: Path,
) -> tuple[Path, str]:
    """构建并保存数据集，返回 (manifest_path, dataset_hash)。"""
    items: list[GroundTruthItem] = []
    exclusions: list[ExcludedItem] = []

    with connect(row_factory=dict) as conn, conn.cursor() as cur:
        for repo in repos:
            rows = _fetch_issues(cur, repo, limit_per_repo)
            for row in rows:
                labels = _labels_of(row)
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
                        github_created_at=str(row["github_created_at"] or ""),
                    )
                )

    # 稳定打乱（可复现），按 split 划分。
    rng = random.Random(seed)
    items.sort(key=lambda item: (item.repo, item.issue_number))
    rng.shuffle(items)

    n_dev = max(1, int(len(items) * 0.8))
    selected = items[:n_dev] if split == "dev" else items[n_dev:]

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
    args = parser.parse_args()

    repos = [item.strip() for item in args.repos.split(",") if item.strip()]
    path, dataset_hash = build_dataset(
        repos=repos,
        limit_per_repo=args.limit_per_repo,
        split=args.split,
        seed=args.seed,
        out_dir=args.out_dir,
    )
    print(f"数据集写入: {path}")
    print(f"SHA-256: {dataset_hash}")


if __name__ == "__main__":
    main()
