#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from psycopg.rows import dict_row  # noqa: E402

from app.db.connection import connect  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="导出待人工标注的 Eval 候选")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--repo")
    args = parser.parse_args()
    query = """
        SELECT DISTINCT ON (repo, issue_number)
               id, repo, issue_number, issue_title, issue_body
        FROM issue_events
    """
    params = []
    if args.repo:
        query += " WHERE repo = %s"
        params.append(args.repo)
    query += " ORDER BY repo, issue_number, id DESC LIMIT %s"
    params.append(max(1, min(args.limit, 1000)))
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for row in rows:
            candidate = {
                "id": f"issue-event-{row['id']}",
                "input": {
                    "repo": row["repo"],
                    "issue_number": row["issue_number"],
                    "title": row["issue_title"],
                    "body": row["issue_body"] or "",
                },
                "expected": {
                    "category": "REQUIRES_HUMAN_LABEL",
                    "priority": "REQUIRES_HUMAN_LABEL",
                    "risk_level": "REQUIRES_HUMAN_LABEL",
                },
                "metadata": {"source": "issueflow_database", "annotator": None},
            }
            output.write(json.dumps(candidate, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
