#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from app.core.config import get_settings
from app.rag.ground_truth import build_ground_truth_bundle, write_ground_truth_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="只读采集维护者明确标记的重复 Issue")
    parser.add_argument("--allow-github-network", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("../eval/datasets"))
    parser.add_argument("--query-limit-per-repo", type=int)
    parser.add_argument("--corpus-limit-per-repo", type=int)
    parser.add_argument("--search-limit-per-repo", type=int, default=350)
    args = parser.parse_args()
    if not args.allow_github_network:
        parser.error("该命令只读访问 GitHub，必须显式提供 --allow-github-network")
    settings = get_settings()
    bundle = build_ground_truth_bundle(
        settings.evaluation_repositories,
        query_limit_per_repo=args.query_limit_per_repo or settings.eval_query_limit_per_repo,
        corpus_limit_per_repo=(
            args.corpus_limit_per_repo or settings.eval_corpus_limit_per_repo
        ),
        search_limit_per_repo=args.search_limit_per_repo,
    )
    write_ground_truth_bundle(bundle, args.output_dir)
    print(json.dumps({
        "repos": settings.evaluation_repositories,
        "dev_queries": len(bundle.dev),
        "test_queries": len(bundle.test),
        "excluded": sum(item["exclusion_reason"] is not None for item in bundle.audit),
        "github_methods": ["GET"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
