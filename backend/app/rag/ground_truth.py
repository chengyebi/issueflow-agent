import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

from app.db.connection import connect
from app.rag.sync import sync_repository_issues
from app.rag.text import normalize_text
from app.services.github import (
    get_repository_issue,
    list_issue_comments,
    list_issue_events,
    list_repository_labels,
    search_issues,
)

MAINTAINER_ASSOCIATIONS = {"MEMBER", "OWNER", "COLLABORATOR"}
DUPLICATE_REFERENCE = re.compile(
    r"(?i)(?:/duplicate\s+of|duplicate(?:d)?\s+of)\s+"
    r"(?:(?:https://github\.com/[\w.-]+/[\w.-]+/issues/)|(?:[\w.-]+/[\w.-]+)?#)(\d+)"
)


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_issue(item: dict) -> bool:
    return item.get("pull_request") is None


def _target_from_event(event: dict) -> int | None:
    source = event.get("source") or {}
    issue = source.get("issue") or {}
    if issue.get("number"):
        return int(issue["number"])
    match = DUPLICATE_REFERENCE.search(event.get("body") or "")
    return int(match.group(1)) if match else None


def extract_active_relations(repo: str, issue: dict, timeline: list[dict]) -> list[dict]:
    """Return active, maintainer-grounded relations in chronological order."""
    active: dict[int, dict] = {}
    for event in sorted(timeline, key=lambda item: item.get("created_at") or ""):
        kind = event.get("event")
        target = _target_from_event(event)
        if kind == "unmarked_as_duplicate":
            if target is None:
                active.clear()
            else:
                active.pop(target, None)
            continue
        accepted = kind == "marked_as_duplicate"
        if kind == "commented":
            accepted = (
                target is not None
                and event.get("author_association") in MAINTAINER_ASSOCIATIONS
            )
        if not accepted or target is None or target == int(issue["number"]):
            continue
        body = normalize_text(event.get("body"))
        active[target] = {
            "repo": repo,
            "query_issue_number": int(issue["number"]),
            "target_issue_number": target,
            "evidence_source": kind,
            "evidence_event_id": str(event.get("id") or ""),
            "evidence_time": event.get("created_at"),
            "operator_login": (event.get("actor") or event.get("user") or {}).get("login"),
            "operator_association": event.get("author_association") or "write_access_event",
            "evidence_excerpt": body[:240],
        }
    return list(active.values())


class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, value: int) -> int:
        self.parent.setdefault(value, value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


@dataclass
class GroundTruthBundle:
    dev: list[dict]
    test: list[dict]
    clusters: dict
    audit: list[dict]
    snapshots: dict


def _candidate_issues(repo: str, search_limit: int) -> list[dict]:
    labels = [
        str(label.get("name"))
        for label in list_repository_labels(repo)
        if "duplicate" in str(label.get("name", "")).casefold()
    ]
    found: dict[int, dict] = {}
    per_query = min(search_limit, 1000)
    for label in sorted(labels)[:4]:
        query = f'repo:{repo} is:issue is:closed label:"{label}"'
        for item in search_issues(
            query, max_items=per_query, sort="created", order="desc"
        ):
            if _is_issue(item):
                found[int(item["number"])] = item
    comment_query = f'repo:{repo} is:issue is:closed duplicate in:comments'
    for item in search_issues(
        comment_query, max_items=per_query, sort="created", order="desc"
    ):
        if _is_issue(item):
            found[int(item["number"])] = item
    return sorted(found.values(), key=lambda item: item.get("updated_at") or "", reverse=True)


def _time_aligned_corpus(repo: str, before: datetime, limit: int) -> list[dict]:
    """Collect a bounded corpus entirely available before the earliest query."""
    found: dict[int, dict] = {}
    boundary = before.date().isoformat()
    while len(found) < limit:
        batch = search_issues(
            f"repo:{repo} is:issue created:<{boundary}",
            max_items=min(1000, limit - len(found)),
            sort="created",
            order="desc",
        )
        issues = [item for item in batch if _is_issue(item)]
        if not issues:
            break
        for item in issues:
            found[int(item["number"])] = item
        oldest = min(_iso(item["created_at"]) for item in issues)
        next_boundary = oldest.date().isoformat()
        if next_boundary == boundary:
            # Excluding the whole boundary day avoids duplicates and guarantees
            # forward progress; the omitted same-day tail is recorded by policy.
            next_boundary = (oldest.date()).isoformat()
        if next_boundary >= boundary:
            break
        boundary = next_boundary
    return sorted(found.values(), key=lambda item: item["created_at"], reverse=True)[:limit]


def _candidate_relations(repo: str, summary: dict) -> list[dict]:
    comments = [
        {**comment, "event": "commented", "actor": comment.get("user")}
        for comment in list_issue_comments(repo, int(summary["number"]), max_items=100)
    ]
    events = list_issue_events(repo, int(summary["number"]), max_items=100)
    # Events have priority and can cancel an older relation. Comments are included
    # only after association filtering in extract_active_relations.
    return extract_active_relations(repo, summary, [*comments, *events])


def collect_repository_ground_truth(
    repo: str,
    *,
    query_limit: int = 50,
    search_limit: int = 350,
) -> tuple[list[dict], list[dict], dict[int, dict]]:
    if not 1 <= query_limit <= 100:
        raise ValueError("query_limit 必须在 1 到 100 之间")
    issue_cache: dict[int, dict] = {}
    edges: list[dict] = []
    audit: list[dict] = []
    accepted_queries: set[int] = set()
    candidates = _candidate_issues(repo, search_limit)
    scanned = 0
    for offset in range(0, len(candidates), 20):
        batch = candidates[offset : offset + 20]
        # Two bounded read-only requests in parallel keep public API traffic modest
        # while avoiding Docker network latency dominating the snapshot build.
        with ThreadPoolExecutor(max_workers=2) as executor:
            relations_by_issue = list(
                executor.map(lambda item: _candidate_relations(repo, item), batch)
            )
        for summary, relations in zip(batch, relations_by_issue, strict=True):
            scanned += 1
            number = int(summary["number"])
            if relations:
                issue_cache[number] = summary
            for relation in relations:
                target_number = relation["target_issue_number"]
                target = issue_cache.get(target_number)
                if target is None:
                    target = get_repository_issue(repo, target_number)
                    issue_cache[target_number] = target
                reason = None
                if not _is_issue(target):
                    reason = "target_is_pull_request"
                elif _iso(target["created_at"]) >= _iso(summary["created_at"]):
                    reason = "target_not_older_than_query"
                target_pattern = re.compile(rf"(?<!\d)#?{target_number}(?!\d)")
                if target_pattern.search(
                    f"{summary.get('title') or ''}\n{summary.get('body') or ''}"
                ):
                    reason = "query_title_or_body_contains_target"
                record = {
                    **relation,
                    "query_created_at": summary["created_at"],
                    "query_title": normalize_text(summary.get("title")),
                    "query_body": normalize_text(summary.get("body")),
                    "query_url": summary.get("html_url"),
                    "target_created_at": target.get("created_at"),
                    "target_title": normalize_text(target.get("title")),
                    "target_url": target.get("html_url"),
                    "leakage_risk": reason == "query_title_or_body_contains_target",
                    "exclusion_reason": reason,
                }
                audit.append(record)
                if reason is None:
                    edges.append(record)
                    accepted_queries.add(number)
        print(
            f"ground-truth repo={repo} scanned={scanned}/{len(candidates)} "
            f"valid_queries={len(accepted_queries)}",
            file=sys.stderr,
            flush=True,
        )
        if len(accepted_queries) >= query_limit:
            break
    return edges, audit, issue_cache


def build_ground_truth_bundle(
    repos: list[str], *, query_limit_per_repo: int, corpus_limit_per_repo: int,
    search_limit_per_repo: int = 350,
) -> GroundTruthBundle:
    all_edges: list[dict] = []
    all_audit: list[dict] = []
    snapshot_time = datetime.now(timezone.utc).isoformat()
    snapshots = {
        "schema_version": "1.0",
        "snapshot_time": snapshot_time,
        "retrieval_input_fields": ["title", "body"],
        "repos": {},
    }
    for repo in repos:
        edges, audit, issue_cache = collect_repository_ground_truth(
            repo, query_limit=query_limit_per_repo, search_limit=search_limit_per_repo
        )
        print(
            f"ground-truth repo={repo} completed valid_queries="
            f"{len({edge['query_issue_number'] for edge in edges})}",
            file=sys.stderr,
            flush=True,
        )
        all_edges.extend(edges)
        all_audit.extend(audit)
        # Ground-truth endpoints can be older than the bounded recent corpus. Upsert
        # them first, then add the bounded repository snapshot.
        def grounded_fetcher(_repo: str, _cache=issue_cache, **_kwargs):
            return list(_cache.values())

        grounded_sync = sync_repository_issues(
            repo,
            fetcher=grounded_fetcher,
            max_issues=max(1, len(issue_cache)),
        )
        earliest_query = min(_iso(edge["query_created_at"]) for edge in edges)
        aligned_corpus = _time_aligned_corpus(
            repo, earliest_query, corpus_limit_per_repo
        )

        def corpus_fetcher(_repo: str, _items=aligned_corpus, **_kwargs):
            return _items

        sync = sync_repository_issues(
            repo, fetcher=corpus_fetcher, max_issues=corpus_limit_per_repo
        )
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS issue_count,
                       min(github_created_at) AS earliest_created_at,
                       max(github_created_at) AS latest_created_at
                FROM historical_issues WHERE repo = %s
                """,
                (repo,),
            )
            corpus = cur.fetchone()
        snapshots["repos"][repo] = {
            "requested_corpus_limit": corpus_limit_per_repo,
            "corpus_cutoff_before": earliest_query.isoformat(),
            "time_aligned_corpus_fetched": len(aligned_corpus),
            "same_day_boundary_policy": "exclude boundary day between search windows",
            "ground_truth_issue_cache_count": len(issue_cache),
            "valid_edge_count": len(edges),
            "excluded_edge_count": sum(item["exclusion_reason"] is not None for item in audit),
            "sync": sync,
            "ground_truth_sync": grounded_sync,
            "stored_issue_count": corpus["issue_count"],
            "earliest_created_at": corpus["earliest_created_at"].isoformat(),
            "latest_created_at": corpus["latest_created_at"].isoformat(),
        }

    by_repo: dict[str, _UnionFind] = {repo: _UnionFind() for repo in repos}
    for edge in all_edges:
        by_repo[edge["repo"]].union(
            edge["query_issue_number"], edge["target_issue_number"]
        )
    cluster_members: dict[str, list[int]] = {}
    for repo, union in by_repo.items():
        for issue_number in union.parent:
            key = f"{repo}:{union.find(issue_number)}"
            cluster_members.setdefault(key, []).append(issue_number)
    cluster_members = {key: sorted(set(value)) for key, value in cluster_members.items()}

    edge_by_query: dict[tuple[str, int], list[dict]] = {}
    created: dict[tuple[str, int], datetime] = {}
    for edge in all_edges:
        edge_by_query.setdefault((edge["repo"], edge["query_issue_number"]), []).append(edge)
        created[(edge["repo"], edge["query_issue_number"])] = _iso(edge["query_created_at"])
        created[(edge["repo"], edge["target_issue_number"])] = _iso(edge["target_created_at"])

    dev: list[dict] = []
    test: list[dict] = []
    for (repo, query_number), query_edges in sorted(edge_by_query.items()):
        root = by_repo[repo].find(query_number)
        cluster_id = f"{repo}:{root}"
        query_time = created[(repo, query_number)]
        relevant = [
            number
            for number in cluster_members[cluster_id]
            if number != query_number
            and created.get((repo, number), query_time) < query_time
        ]
        if not relevant:
            continue
        primary = query_edges[0]
        record = {
            "schema_version": "1.0",
            "id": f"{repo}#{query_number}",
            "repo": repo,
            "query_issue_number": query_number,
            "query_created_at": primary["query_created_at"],
            "query_title": primary["query_title"],
            "query_body": primary["query_body"],
            "query_url": primary["query_url"],
            "cluster_id": cluster_id,
            "relevant_issue_numbers": sorted(relevant),
            "evidence": [
                {key: edge[key] for key in (
                    "target_issue_number", "evidence_source", "evidence_event_id",
                    "evidence_time", "operator_login", "operator_association",
                    "evidence_excerpt",
                )}
                for edge in query_edges
            ],
            "retrieval_input_fields": ["title", "body"],
        }
        bucket = int(hashlib.sha256(cluster_id.encode()).hexdigest()[:8], 16) % 10
        (dev if bucket < 4 else test).append(record)
    return GroundTruthBundle(
        dev=dev,
        test=test,
        clusters={
            "schema_version": "1.0",
            "snapshot_time": snapshot_time,
            "clusters": cluster_members,
            "audit_exclusions": all_audit,
        },
        audit=all_audit,
        snapshots=snapshots,
    )


def write_ground_truth_bundle(bundle: GroundTruthBundle, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, records in (
        ("duplicate_qrels_dev.jsonl", bundle.dev),
        ("duplicate_qrels_test.jsonl", bundle.test),
    ):
        (directory / name).write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )
    (directory / "duplicate_clusters.json").write_text(
        json.dumps(bundle.clusters, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "repository_snapshots.json").write_text(
        json.dumps(bundle.snapshots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
