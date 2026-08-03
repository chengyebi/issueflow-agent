import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from app.rag.embedding import get_embedding_provider
from app.rag.sync import sync_repository_issues
from app.rag.text import normalize_labels, normalize_text
from app.services.github import (
    get_repository_issue,
    list_issue_comments,
    list_issue_events,
    list_repository_issues,
    search_issues,
)

REPOSITORY = "microsoft/vscode"
ISSUE_URL = "https://github.com/microsoft/vscode/issues/{number}"
DIRECT_DUPLICATE_PATTERN = re.compile(
    r"(?i)(?:/duplicate(?:\s+of)?|duplicate(?:d)?\s+(?:of\s+)?|same\s+as\s+)"
    r"(?:https://github\.com/microsoft/vscode/issues/|microsoft/vscode#|#)(\d+)"
)
TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.+-]{1,}")
STOP_WORDS = {
    "and", "are", "but", "for", "from", "has", "have", "issue", "not", "that",
    "the", "this", "with", "vscode", "when", "will", "after", "before", "into",
}


@dataclass(frozen=True)
class LabelCandidate:
    query_repo: str
    query_issue_number: int
    query_title: str
    query_url: str
    proposed_duplicate_issue_number: int
    proposed_duplicate_title: str
    proposed_duplicate_url: str
    extraction_source: str
    extraction_evidence: str
    query_labels: list[str]
    candidate_labels: list[str]
    proposed_label: str
    human_label: str
    human_notes: str
    split: str
    source_snapshot_time: str
    candidate_kind: str
    ambiguity_reason: str


def _labels(issue: dict) -> list[str]:
    return normalize_labels(
        [
            str(label.get("name", "")) if isinstance(label, dict) else str(label)
            for label in issue.get("labels", [])
        ]
    )


def _is_pull_request(issue: dict) -> bool:
    return issue.get("pull_request") is not None


def _extract_reference(text: str | None) -> tuple[int, str] | None:
    normalized = normalize_text(text)
    match = DIRECT_DUPLICATE_PATTERN.search(normalized)
    if match is None:
        return None
    start = max(0, match.start() - 100)
    end = min(len(normalized), match.end() + 180)
    return int(match.group(1)), normalized[start:end].replace("\n", " ")


def _find_duplicate_reference(issue: dict) -> tuple[int, str, str] | None:
    body_match = _extract_reference(issue.get("body"))
    if body_match:
        return body_match[0], "issue_body", body_match[1]
    number = int(issue["number"])
    for comment in list_issue_comments(REPOSITORY, number, max_items=100):
        comment_match = _extract_reference(comment.get("body"))
        if comment_match:
            return comment_match[0], "issue_comment", comment_match[1]
    for event in list_issue_events(REPOSITORY, number, max_items=100):
        event_text = json.dumps(event, ensure_ascii=False, default=str)
        event_match = _extract_reference(event_text)
        if event_match:
            return event_match[0], "issue_event", event_match[1]
    return None


def _terms(issue: dict) -> set[str]:
    text = f"{issue.get('title', '')} {' '.join(_labels(issue))}".casefold()
    return {token for token in TOKEN_PATTERN.findall(text) if token not in STOP_WORDS}


def _similarity(left: dict, right: dict) -> tuple[float, list[str]]:
    left_terms = _terms(left)
    right_terms = _terms(right)
    union = left_terms | right_terms
    intersection = left_terms & right_terms
    jaccard = len(intersection) / len(union) if union else 0.0
    title_ratio = SequenceMatcher(
        None,
        normalize_text(left.get("title")).casefold(),
        normalize_text(right.get("title")).casefold(),
    ).ratio()
    return 0.7 * jaccard + 0.3 * title_ratio, sorted(intersection)


def _candidate(
    query: dict,
    proposed: dict,
    *,
    source: str,
    evidence: str,
    proposed_label: str,
    snapshot: str,
    kind: str,
    ambiguity_reason: str = "",
) -> LabelCandidate:
    return LabelCandidate(
        query_repo=REPOSITORY,
        query_issue_number=int(query["number"]),
        query_title=normalize_text(query.get("title")),
        query_url=query.get("html_url") or ISSUE_URL.format(number=query["number"]),
        proposed_duplicate_issue_number=int(proposed["number"]),
        proposed_duplicate_title=normalize_text(proposed.get("title")),
        proposed_duplicate_url=(
            proposed.get("html_url") or ISSUE_URL.format(number=proposed["number"])
        ),
        extraction_source=source,
        extraction_evidence=evidence[:500],
        query_labels=_labels(query),
        candidate_labels=_labels(proposed),
        proposed_label=proposed_label,
        human_label="",
        human_notes="",
        split="",
        source_snapshot_time=snapshot,
        candidate_kind=kind,
        ambiguity_reason=ambiguity_reason,
    )


def _write_candidates(candidates: list[LabelCandidate], jsonl_path: Path, csv_path: Path) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(candidate) for candidate in candidates]
    jsonl_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    fieldnames = list(rows[0])
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "query_labels": json.dumps(row["query_labels"], ensure_ascii=False),
                    "candidate_labels": json.dumps(row["candidate_labels"], ensure_ascii=False),
                }
            )


def build_label_package(
    *,
    duplicate_search_limit: int,
    duplicate_target: int,
    background_limit: int,
    hard_negative_target: int,
    ordinary_negative_target: int,
) -> tuple[list[LabelCandidate], list[dict]]:
    snapshot = datetime.now(timezone.utc).isoformat()
    duplicate_issues = search_issues(
        f"repo:{REPOSITORY} is:issue is:closed label:*duplicate",
        max_items=duplicate_search_limit,
    )
    duplicate_pairs: list[tuple[dict, dict, str, str]] = []
    issues_by_number: dict[int, dict] = {}
    for issue in duplicate_issues:
        if _is_pull_request(issue):
            continue
        relation = _find_duplicate_reference(issue)
        if relation is None or relation[0] == int(issue["number"]):
            continue
        original = get_repository_issue(REPOSITORY, relation[0])
        if _is_pull_request(original):
            continue
        duplicate_pairs.append((issue, original, relation[1], relation[2]))
        issues_by_number[int(issue["number"])] = issue
        issues_by_number[int(original["number"])] = original
        if len(duplicate_pairs) >= duplicate_target:
            break
    if len(duplicate_pairs) < duplicate_target:
        raise RuntimeError(
            f"只提取到 {len(duplicate_pairs)} 条显式重复关系，低于目标 {duplicate_target}"
        )

    background = []
    for scanned, issue in enumerate(
        list_repository_issues(REPOSITORY, state="closed", per_page=100), start=1
    ):
        if scanned > background_limit:
            break
        if _is_pull_request(issue) or "*duplicate" in _labels(issue):
            continue
        background.append(issue)

    known_pairs = {
        (int(query["number"]), int(original["number"]))
        for query, original, _, _ in duplicate_pairs
    }
    candidates = [
        _candidate(
            query,
            original,
            source=source,
            evidence=evidence,
            proposed_label="duplicate",
            snapshot=snapshot,
            kind="extracted_duplicate",
        )
        for query, original, source, evidence in duplicate_pairs
    ]

    ranked_pairs = []
    for query, _, _, _ in duplicate_pairs:
        for proposed in background:
            pair = (int(query["number"]), int(proposed["number"]))
            if pair in known_pairs or pair[0] == pair[1]:
                continue
            score, shared_terms = _similarity(query, proposed)
            ranked_pairs.append((score, pair, shared_terms, query, proposed))
    ranked_pairs.sort(key=lambda item: (-item[0], item[1]))

    used_pairs = set(known_pairs)
    hard_count = 0
    for score, pair, shared_terms, query, proposed in ranked_pairs:
        if pair in used_pairs or score < 0.14:
            continue
        candidates.append(
            _candidate(
                query,
                proposed,
                source="deterministic_title_label_similarity",
                evidence=(
                    f"hard-negative score={score:.4f}; shared_terms="
                    f"{','.join(shared_terms[:12]) or '[none]'}"
                ),
                proposed_label="non_duplicate",
                snapshot=snapshot,
                kind="hard_negative",
                ambiguity_reason="主题相近且未发现显式重复引用，必须人工确认并排除隐藏重复关系",
            )
        )
        issues_by_number[int(proposed["number"])] = proposed
        used_pairs.add(pair)
        hard_count += 1
        if hard_count >= hard_negative_target:
            break
    if hard_count < hard_negative_target:
        raise RuntimeError(f"只生成 {hard_count} 条困难负样本，低于目标 {hard_negative_target}")

    ordinary_count = 0
    for score, pair, _shared_terms, query, proposed in reversed(ranked_pairs):
        if pair in used_pairs or score > 0.08:
            continue
        candidates.append(
            _candidate(
                query,
                proposed,
                source="deterministic_low_similarity_baseline",
                evidence=f"ordinary-negative score={score:.4f}; shared_terms=[none]",
                proposed_label="non_duplicate",
                snapshot=snapshot,
                kind="ordinary_negative",
            )
        )
        issues_by_number[int(proposed["number"])] = proposed
        used_pairs.add(pair)
        ordinary_count += 1
        if ordinary_count >= ordinary_negative_target:
            break
    if ordinary_count < ordinary_negative_target:
        raise RuntimeError(
            f"只生成 {ordinary_count} 条普通负样本，低于目标 {ordinary_negative_target}"
        )
    return candidates, list(issues_by_number.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 microsoft/vscode 查重人工标注包")
    parser.add_argument("--allow-github-network", action="store_true")
    parser.add_argument("--embed", action="store_true")
    parser.add_argument("--duplicate-search-limit", type=int, default=70)
    parser.add_argument("--duplicate-target", type=int, default=30)
    parser.add_argument("--background-limit", type=int, default=300)
    parser.add_argument("--hard-negative-target", type=int, default=20)
    parser.add_argument("--ordinary-negative-target", type=int, default=10)
    parser.add_argument("--jsonl", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()
    if not args.allow_github_network:
        parser.error("该命令只读访问 GitHub，必须显式提供 --allow-github-network")
    if args.duplicate_search_limit > 100 or args.background_limit > 500:
        parser.error("为避免无边界下载，duplicate-search-limit<=100 且 background-limit<=500")

    candidates, issues = build_label_package(
        duplicate_search_limit=args.duplicate_search_limit,
        duplicate_target=args.duplicate_target,
        background_limit=args.background_limit,
        hard_negative_target=args.hard_negative_target,
        ordinary_negative_target=args.ordinary_negative_target,
    )
    provider = get_embedding_provider() if args.embed else None

    def selected_fetcher(repo: str, **kwargs):
        if repo != REPOSITORY:
            raise ValueError("标注包 Backfill 仅允许 microsoft/vscode")
        return issues

    sync_result = sync_repository_issues(
        REPOSITORY,
        fetcher=selected_fetcher,
        embedding_provider=provider,
        max_issues=len(issues),
    )
    _write_candidates(candidates, args.jsonl, args.csv)
    counts = {
        kind: sum(candidate.candidate_kind == kind for candidate in candidates)
        for kind in ("extracted_duplicate", "hard_negative", "ordinary_negative")
    }
    print(
        json.dumps(
            {
                "repo": REPOSITORY,
                "candidate_count": len(candidates),
                "candidate_counts": counts,
                "backfill": sync_result,
                "jsonl": str(args.jsonl),
                "csv": str(args.csv),
                "ground_truth": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
