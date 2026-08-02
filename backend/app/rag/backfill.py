import argparse
import json

from app.rag.embedding import create_embedding_provider
from app.rag.sync import sync_repository_issues


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 GitHub 仓库历史 Issue")
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--embed", action="store_true")
    parser.add_argument("--allow-github-network", action="store_true")
    args = parser.parse_args()
    if not args.allow_github_network:
        parser.error("Backfill 会访问 GitHub，必须显式提供 --allow-github-network")
    provider = create_embedding_provider() if args.embed else None
    result = sync_repository_issues(args.repo, embedding_provider=provider)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
