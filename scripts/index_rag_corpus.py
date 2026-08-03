#!/usr/bin/env python3
import argparse
import json

from app.core.config import get_settings
from app.rag.embedding import get_embedding_provider
from app.rag.indexing import embed_repository_issues


def main() -> None:
    parser = argparse.ArgumentParser(description="批量生成 head512 与 Chunk Embedding")
    parser.add_argument("--repo", action="append")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    settings = get_settings()
    provider = get_embedding_provider()
    results = [
        embed_repository_issues(
            repo,
            limit=args.limit or settings.eval_corpus_limit_per_repo,
            provider=provider,
            settings=settings,
        )
        for repo in (args.repo or settings.evaluation_repositories)
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
