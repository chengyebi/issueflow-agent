#!/usr/bin/env python3
"""Build the human-readable and machine-readable retrieval diagnostic outputs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


K_VALUES = (1, 5, 10, 20, 30, 50, 100)
METHODS = (
    "lexical",
    "vector_head512",
    "vector_chunked",
    "hybrid_head512_rrf",
    "hybrid_chunked_rrf",
)
REPOS = ("microsoft/vscode", "nodejs/node", "rust-lang/rust")
KEY_METHODS = (
    "vector_head512",
    "vector_chunked",
    "hybrid_head512_rrf",
    "hybrid_chunked_rrf",
)
METHOD_LABELS = {
    "lexical": "lexical",
    "vector_head512": "vector_head512",
    "vector_chunked": "vector_chunked",
    "hybrid_head512_rrf": "hybrid_head512_rrf",
    "hybrid_chunked_rrf": "hybrid_chunked_rrf",
    "current_online_default": "CURRENT ONLINE DEFAULT",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def percentile(values: list[float], fraction: float):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def mean(values: list[float]):
    return statistics.fmean(values) if values else None


def valid_records(records: list[dict]) -> list[dict]:
    return [record for record in records if "ranked" in record]


def summarize(records: list[dict]) -> dict:
    records = valid_records(records)
    return {
        "query_count": len(records),
        "recall": {
            str(k): mean([record["recall"][str(k)] for record in records])
            for k in K_VALUES
        },
        "hit_rate": {
            str(k): mean([float(record["hit"][str(k)]) for record in records])
            for k in K_VALUES
        },
        "hit_count": {
            str(k): sum(bool(record["hit"][str(k)]) for record in records)
            for k in K_VALUES
        },
        "p50_latency_ms": percentile(
            [record["latency_ms"] for record in records], 0.50
        ),
        "p95_latency_ms": percentile(
            [record["latency_ms"] for record in records], 0.95
        ),
        "degraded_count": sum(bool(record.get("degraded")) for record in records),
        "min_returned_candidates": min(
            (len(record["ranked"]) for record in records), default=0
        ),
        "median_returned_candidates": percentile(
            [len(record["ranked"]) for record in records], 0.50
        ),
    }


def grouped_summary(records: list[dict]) -> dict:
    records = valid_records(records)
    per_repo = {
        repo: summarize([record for record in records if record["repo"] == repo])
        for repo in REPOS
    }
    macro = {
        "repo_count": len(REPOS),
        "query_count": len(records),
        "recall": {
            str(k): mean([per_repo[repo]["recall"][str(k)] for repo in REPOS])
            for k in K_VALUES
        },
        "hit_rate": {
            str(k): mean([per_repo[repo]["hit_rate"][str(k)] for repo in REPOS])
            for k in K_VALUES
        },
    }
    return {"overall": summarize(records), "macro_average": macro, "per_repo": per_repo}


def first_rank_summary(records: list[dict]) -> dict:
    records = valid_records(records)
    ranks = [record["first_relevant_rank"] for record in records]
    found = [rank for rank in ranks if rank is not None]
    total = len(records)
    counts = {
        "rank_1": sum(rank == 1 for rank in ranks),
        **{
            "le_%s" % k: sum(rank is not None and rank <= k for rank in ranks)
            for k in (5, 10, 20, 30, 50, 100)
        },
        "gt_100_or_not_found": sum(rank is None for rank in ranks),
    }
    return {
        "query_count": total,
        "counts": counts,
        "rates": {key: value / total if total else None for key, value in counts.items()},
        "found_within_100": len(found),
        "percentiles_among_found": {
            "median": percentile(found, 0.50),
            "p75": percentile(found, 0.75),
            "p90": percentile(found, 0.90),
            "p95": percentile(found, 0.95),
        },
    }


def threshold_k(summary: dict, metric: str, threshold: float) -> str:
    for k in K_VALUES:
        value = summary[metric][str(k)]
        if value is not None and value >= threshold:
            return "K=%s" % k
    return "未达到"


def by_id(records: list[dict]) -> dict[str, dict]:
    return {record["id"]: record for record in valid_records(records)}


def pair_counts(first_records: list[dict], second_records: list[dict], k: int) -> dict:
    first = by_id(first_records)
    second = by_id(second_records)
    ids = sorted(set(first) & set(second))
    output = {"second_rescue": 0, "second_harm": 0, "both_hit": 0, "both_miss": 0}
    for case_id in ids:
        first_hit = bool(first[case_id]["hit"][str(k)])
        second_hit = bool(second[case_id]["hit"][str(k)])
        if not first_hit and second_hit:
            output["second_rescue"] += 1
        elif first_hit and not second_hit:
            output["second_harm"] += 1
        elif first_hit and second_hit:
            output["both_hit"] += 1
        else:
            output["both_miss"] += 1
    output["query_count"] = len(ids)
    return output


def rank_movement(first_records: list[dict], second_records: list[dict]) -> dict:
    first = by_id(first_records)
    second = by_id(second_records)
    ids = sorted(set(first) & set(second))
    output = {
        "second_rank_improved": 0,
        "second_rank_declined": 0,
        "unchanged": 0,
        "rescued_from_beyond_100": 0,
        "lost_beyond_100": 0,
    }
    deltas = []
    for case_id in ids:
        first_raw = first[case_id]["first_relevant_rank"]
        second_raw = second[case_id]["first_relevant_rank"]
        first_rank = first_raw if first_raw is not None else 101
        second_rank = second_raw if second_raw is not None else 101
        deltas.append(second_rank - first_rank)
        if second_rank < first_rank:
            output["second_rank_improved"] += 1
        elif second_rank > first_rank:
            output["second_rank_declined"] += 1
        else:
            output["unchanged"] += 1
        if first_raw is None and second_raw is not None:
            output["rescued_from_beyond_100"] += 1
        if first_raw is not None and second_raw is None:
            output["lost_beyond_100"] += 1
    output["query_count"] = len(ids)
    output["median_second_minus_first_rank_capped_101"] = percentile(deltas, 0.50)
    return output


def exact_hnsw_comparison(exact_records: list[dict], hnsw_records: list[dict], k: int) -> dict:
    exact = by_id(exact_records)
    hnsw = by_id(hnsw_records)
    ids = sorted(set(exact) & set(hnsw))
    overlaps = []
    order_matches = 0
    for case_id in ids:
        exact_top = exact[case_id]["ranked"][:k]
        hnsw_top = hnsw[case_id]["ranked"][:k]
        denominator = len(exact_top)
        overlaps.append(
            len(set(exact_top) & set(hnsw_top)) / denominator if denominator else 1.0
        )
        order_matches += exact_top == hnsw_top
    exact_summary = summarize(list(exact.values()))
    hnsw_summary = summarize(list(hnsw.values()))
    return {
        "k": k,
        "query_count": len(ids),
        "hnsw_top_k_recall_relative_to_exact": mean(overlaps),
        "exact_order_match_rate": order_matches / len(ids) if ids else None,
        "exact_hit_rate": exact_summary["hit_rate"][str(k)],
        "hnsw_hit_rate": hnsw_summary["hit_rate"][str(k)],
        "hit_rate_delta_hnsw_minus_exact": (
            hnsw_summary["hit_rate"][str(k)] - exact_summary["hit_rate"][str(k)]
            if ids else None
        ),
        "candidate_hit_pair": pair_counts(list(exact.values()), list(hnsw.values()), k),
        "first_rank_movement": rank_movement(list(exact.values()), list(hnsw.values())),
        "exact_p50_latency_ms": exact_summary["p50_latency_ms"],
        "exact_p95_latency_ms": exact_summary["p95_latency_ms"],
        "hnsw_p50_latency_ms": hnsw_summary["p50_latency_ms"],
        "hnsw_p95_latency_ms": hnsw_summary["p95_latency_ms"],
    }


def length_buckets(head_records: list[dict], chunk_records: list[dict]) -> list[dict]:
    head = by_id(head_records)
    chunk = by_id(chunk_records)
    ordered = sorted(
        (record for case_id, record in head.items() if case_id in chunk),
        key=lambda record: (record["query_token_count"], record["id"]),
    )
    buckets = [[] for _ in range(4)]
    for index, record in enumerate(ordered):
        bucket = min(3, index * 4 // max(1, len(ordered)))
        buckets[bucket].append(record["id"])
    output = []
    for index, ids in enumerate(buckets, 1):
        head_items = [head[case_id] for case_id in ids]
        chunk_items = [chunk[case_id] for case_id in ids]
        head_summary = summarize(head_items)
        chunk_summary = summarize(chunk_items)
        tokens = [head[case_id]["query_token_count"] for case_id in ids]
        item = {
            "bucket": "Q%s" % index,
            "count": len(ids),
            "min_tokens": min(tokens) if tokens else None,
            "max_tokens": max(tokens) if tokens else None,
            "median_tokens": percentile(tokens, 0.50),
            "metrics": {},
        }
        for k in (10, 30, 50, 100):
            item["metrics"][str(k)] = {
                "head_recall": head_summary["recall"][str(k)],
                "chunk_recall": chunk_summary["recall"][str(k)],
                "recall_delta": chunk_summary["recall"][str(k)] - head_summary["recall"][str(k)],
                "head_hit_rate": head_summary["hit_rate"][str(k)],
                "chunk_hit_rate": chunk_summary["hit_rate"][str(k)],
                "hit_rate_delta": chunk_summary["hit_rate"][str(k)] - head_summary["hit_rate"][str(k)],
                "pair": pair_counts(head_items, chunk_items, k),
            }
        output.append(item)
    return output


def text_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", text or "")
        if token.lower() not in {"the", "and", "for", "with", "this", "that", "from", "when"}
    }


def jaccard(first: set[str], second: set[str]) -> float:
    return len(first & second) / len(first | second) if first | second else 0.0


def markdown_escape(value, limit: int = 180) -> str:
    text = " ".join(str(value if value is not None else "—").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def fmt(value, digits: int = 4) -> str:
    return "—" if value is None else ("%%.%sf" % digits) % value


def pct(value, digits: int = 1) -> str:
    return "—" if value is None else ("%%.%sf%%%%" % digits) % (value * 100)


def ms(value) -> str:
    return "—" if value is None else "%.1f" % value


def md_table(headers: list[str], rows: list[list]) -> str:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend(
        "| " + " | ".join(markdown_escape(cell) for cell in row) + " |"
        for row in rows
    )
    return "\n".join(output)


def command_output(command: list[str], cwd: Path) -> tuple[int, str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def case_analysis(case_id: str, qrels: dict, catalog: dict) -> dict:
    qrel = qrels[case_id]
    relevant_titles = [
        catalog.get((qrel["repo"], number), {}).get("title", "本地数据库缺失")
        for number in qrel["relevant_issue_numbers"]
    ]
    query_title_tokens = text_tokens(qrel["query_title"])
    similarities = [
        jaccard(query_title_tokens, text_tokens(title)) for title in relevant_titles
    ]
    title_overlap = max(similarities, default=0.0)
    body = qrel.get("query_body") or ""
    facts = ["标题词 Jaccard=%.2f" % title_overlap, "正文 %s chars" % len(body)]
    hypotheses = []
    if title_overlap < 0.15:
        hypotheses.append("查询与真值标题字面重合低，可能依赖语义改写而非关键词")
    if len(body) > 6000:
        hypotheses.append("长正文中的日志、模板或环境信息可能稀释核心故障")
    if "```" in body or re.search(r"stack|traceback|backtrace|error:", body, re.I):
        hypotheses.append("代码/错误日志可能带来大量技术词噪声")
    if len(re.findall(r"\b\d+(?:\.\d+)+\b", body)) >= 3:
        hypotheses.append("版本号与环境细节可能产生字面污染")
    if not hypotheses:
        hypotheses.append("当前表示可能未把细粒度触发条件与相邻主题充分区分")
    return {
        "relevant_titles": relevant_titles,
        "facts": "；".join(facts),
        "hypothesis": "；".join(hypotheses[:2]),
        "title_jaccard": title_overlap,
    }


def balanced_select(records: list[dict], count: int, sort_key) -> list[dict]:
    grouped = {
        repo: sorted(
            [record for record in records if record["repo"] == repo],
            key=sort_key,
            reverse=True,
        )
        for repo in REPOS
    }
    selected = []
    while len(selected) < count and any(grouped.values()):
        for repo in REPOS:
            if grouped[repo] and len(selected) < count:
                selected.append(grouped[repo].pop(0))
    return selected


def case_table_rows(records: list[dict], qrels: dict, catalog: dict, include_split=False) -> list[list]:
    rows = []
    for record in records:
        details = case_analysis(record["id"], qrels, catalog)
        wrong = [
            "#%s %s" % (item["issue_number"], item["title"])
            for item in record.get("top_candidates", [])
            if item["issue_number"] not in set(record["relevant"])
        ][:3]
        relevant = [
            "#%s %s" % (number, title)
            for number, title in zip(record["relevant"], details["relevant_titles"])
        ]
        row = []
        if include_split:
            row.append(record.get("_split", "—").upper())
        row.extend(
            [
                record["repo"],
                "#%s %s" % (record["query_issue_number"], record["query_title"]),
                "; ".join(relevant),
                record["first_relevant_rank"] or ">100/未找到",
                "; ".join(wrong) or "—",
                details["facts"],
                details["hypothesis"],
            ]
        )
        rows.append(row)
    return rows


def build_analysis(raw: dict, online: dict, ann_probe: dict, context: dict, qrels_by_split: dict) -> dict:
    metrics = {"dev": {}, "test": {}, "combined": {}}
    first_ranks = {"dev": {}, "test": {}, "combined": {}}
    records = {"dev": {}, "test": {}, "combined": {}}
    for split in ("dev", "test"):
        for method in METHODS:
            method_records = valid_records(raw["runs"][split]["exact"].get(method, []))
            records[split][method] = method_records
            metrics[split][method] = grouped_summary(method_records)
            first_ranks[split][method] = first_rank_summary(method_records)
    for method in METHODS:
        combined = [
            {**record, "_split": split}
            for split in ("dev", "test")
            for record in records[split][method]
        ]
        records["combined"][method] = combined
        metrics["combined"][method] = grouped_summary(combined)
        first_ranks["combined"][method] = first_rank_summary(combined)

    online_records = {
        split: valid_records(online["runs"].get(split, [])) for split in ("dev", "test")
    }
    online_top5_records = {
        split: valid_records(online["configured_top5_runs"].get(split, []))
        for split in ("dev", "test")
    }
    online_records["combined"] = [
        {**record, "_split": split}
        for split in ("dev", "test")
        for record in online_records[split]
    ]
    online_metrics = {
        split: grouped_summary(split_records)
        for split, split_records in online_records.items()
    }
    online_top5_metrics = {
        split: grouped_summary(split_records)
        for split, split_records in online_top5_records.items()
    }
    online_effective_metrics = {}
    for split in ("dev", "test"):
        expanded = online_metrics[split]["overall"]
        configured = online_top5_metrics[split]["overall"]
        effective = json.loads(json.dumps(expanded))
        for metric in ("recall", "hit_rate", "hit_count"):
            for k in (1, 5):
                effective[metric][str(k)] = configured[metric][str(k)]
        online_effective_metrics[split] = effective

    head_chunk = {}
    lexical_vector = {}
    rrf = {}
    exact_hnsw = {}
    matched_ann_latency = {}
    for split in ("dev", "test", "combined"):
        head = records[split]["vector_head512"]
        chunk = records[split]["vector_chunked"]
        head_chunk[split] = {
            "pair_by_k": {str(k): pair_counts(head, chunk, k) for k in K_VALUES},
            "rank_movement_chunk_minus_head": rank_movement(head, chunk),
            "length_buckets": length_buckets(head, chunk),
        }
        lexical_vector[split] = {}
        for vector_method in ("vector_head512", "vector_chunked"):
            lexical_vector[split][vector_method] = {
                str(k): pair_counts(
                    records[split][vector_method], records[split]["lexical"], k
                )
                for k in (10, 30, 50)
            }
        rrf[split] = {}
        for vector_method, hybrid_method in (
            ("vector_head512", "hybrid_head512_rrf"),
            ("vector_chunked", "hybrid_chunked_rrf"),
        ):
            rrf[split][hybrid_method] = {
                "pair_by_k": {
                    str(k): pair_counts(
                        records[split][vector_method], records[split][hybrid_method], k
                    )
                    for k in (5, 10, 30, 50)
                },
                "rank_movement": rank_movement(
                    records[split][vector_method], records[split][hybrid_method]
                ),
            }
    for split in ("dev", "test"):
        exact_hnsw[split] = {}
        matched_ann_latency[split] = {}
        for method in ("vector_head512", "vector_chunked"):
            hnsw_records = valid_records(raw["runs"][split]["hnsw"].get(method, []))
            exact_hnsw[split][method] = {
                str(k): exact_hnsw_comparison(records[split][method], hnsw_records, k)
                for k in (10, 30, 50)
            }
            probe_exact = summarize(ann_probe["runs"][split][method]["exact"])
            probe_hnsw = summarize(ann_probe["runs"][split][method]["hnsw"])
            matched_ann_latency[split][method] = {
                "query_count": probe_exact["query_count"],
                "exact_p50_latency_ms": probe_exact["p50_latency_ms"],
                "exact_p95_latency_ms": probe_exact["p95_latency_ms"],
                "hnsw_p50_latency_ms": probe_hnsw["p50_latency_ms"],
                "hnsw_p95_latency_ms": probe_hnsw["p95_latency_ms"],
            }

    strongest = max(
        METHODS,
        key=lambda method: (
            metrics["dev"][method]["overall"]["hit_rate"]["50"],
            metrics["dev"][method]["overall"]["recall"]["50"],
            metrics["dev"][method]["overall"]["hit_rate"]["100"],
            -metrics["dev"][method]["overall"]["p50_latency_ms"],
        ),
    )
    return {
        "schema_version": "diagnostic-analysis-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strongest_method_by_dev_hit50_then_recall50": strongest,
        "metrics": metrics,
        "online_metrics": online_metrics,
        "online_configured_top5_metrics": online_top5_metrics,
        "online_effective_curve": online_effective_metrics,
        "first_relevant_rank": first_ranks,
        "head512_vs_chunked": head_chunk,
        "lexical_vs_vector": lexical_vector,
        "rrf": rrf,
        "exact_vs_hnsw": exact_hnsw,
        "matched_ann_latency": matched_ann_latency,
        "degraded_total": sum(
            bool(record.get("degraded"))
            for split_runs in raw["runs"].values()
            for phase_runs in split_runs.values()
            for method_records in phase_runs.values()
            for record in method_records
            if "ranked" in record
        )
        + sum(
            bool(record.get("degraded"))
            for run_group in (online["runs"], online["configured_top5_runs"])
            for method_records in run_group.values()
            for record in method_records
            if "ranked" in record
        ),
    }


def append_metric_tables(lines: list[str], analysis: dict, split: str) -> None:
    lines.append("### %s Overall（Query 加权）" % split.upper())
    lines.append("")
    for metric, label in (("recall", "Recall"), ("hit_rate", "Candidate Hit Rate")):
        rows = []
        for method in METHODS:
            summary = analysis["metrics"][split][method]["overall"]
            rows.append([method] + [fmt(summary[metric][str(k)]) for k in K_VALUES])
        lines.append(md_table(["Method"] + ["%s@%s" % (label, k) for k in K_VALUES], rows))
        lines.append("")
    lines.append("### %s Macro Average（三仓库等权）" % split.upper())
    lines.append("")
    for metric, label in (("recall", "Recall"), ("hit_rate", "Candidate Hit Rate")):
        rows = []
        for method in METHODS:
            summary = analysis["metrics"][split][method]["macro_average"]
            rows.append([method] + [fmt(summary[metric][str(k)]) for k in K_VALUES])
        lines.append(md_table(["Method"] + ["%s@%s" % (label, k) for k in K_VALUES], rows))
        lines.append("")


def render_report(
    args,
    raw: dict,
    online: dict,
    context: dict,
    snapshots: dict,
    clusters: dict,
    primary: dict,
    verification: dict,
    analysis: dict,
    qrels_by_split: dict,
) -> str:
    all_qrels = {
        qrel["id"]: {**qrel, "_split": split}
        for split, qrels in qrels_by_split.items()
        for qrel in qrels
    }
    catalog = {
        (item["repo"], item["issue_number"]): item for item in context["issue_catalog"]
    }
    git_rc, git_status = command_output(
        ["git", "status", "--short", "--untracked-files=all"], args.repo_root
    )
    _, branch = command_output(["git", "branch", "--show-current"], args.repo_root)
    _, current_sha = command_output(["git", "rev-parse", "HEAD"], args.repo_root)
    _, docker_version = command_output(["docker", "--version"], args.repo_root)
    _, compose_version = command_output(["docker", "compose", "version"], args.repo_root)

    metrics = analysis["metrics"]
    strongest = analysis["strongest_method_by_dev_hit50_then_recall50"]
    dev_strong = metrics["dev"][strongest]["overall"]
    test_strong = metrics["test"][strongest]["overall"]
    dev_online_actual = analysis["online_configured_top5_metrics"]["dev"]["overall"]
    test_online_actual = analysis["online_configured_top5_metrics"]["test"]["overall"]
    dev_online_curve = analysis["online_effective_curve"]["dev"]
    test_online_curve = analysis["online_effective_curve"]["test"]
    dev_hit50 = dev_strong["hit_rate"]["50"]
    dev_hit100 = dev_strong["hit_rate"]["100"]
    reaches_90 = dev_hit50 >= 0.90 or dev_hit100 >= 0.90
    reaches_95 = dev_hit50 >= 0.95 or dev_hit100 >= 0.95
    if dev_hit50 >= 0.90:
        core_diagnosis = "更符合情况 A：强 Retriever 在 Top-50 已有足够候选覆盖，历史 Top-5 截断过早。"
    else:
        core_diagnosis = "更符合情况 B：强 Retriever 到 Top-50 仍低于 90%，不能只归因于 Top-K 截断。"

    head_rrf = analysis["rrf"]["dev"]["hybrid_head512_rrf"]["pair_by_k"]["50"]
    chunk_rrf = analysis["rrf"]["dev"]["hybrid_chunked_rrf"]["pair_by_k"]["50"]
    rrf_harmful = (
        head_rrf["second_harm"] > head_rrf["second_rescue"]
        and chunk_rrf["second_harm"] > chunk_rrf["second_rescue"]
    )
    # Use the paired, consistently throttled probe for cost decisions.  The full
    # run crossed an operator-requested thermal-policy change, so its raw timing
    # is useful provenance but not a fair head-vs-chunk comparison.
    head_latency = analysis["matched_ann_latency"]["dev"]["vector_head512"][
        "exact_p50_latency_ms"
    ]
    chunk_latency = analysis["matched_ann_latency"]["dev"]["vector_chunked"][
        "exact_p50_latency_ms"
    ]
    chunk_delta50 = (
        metrics["dev"]["vector_chunked"]["overall"]["hit_rate"]["50"]
        - metrics["dev"]["vector_head512"]["overall"]["hit_rate"]["50"]
    )
    chunk_ratio = chunk_latency / head_latency if head_latency else None
    dev_buckets = analysis["head512_vs_chunked"]["dev"]["length_buckets"]
    q4_chunk_delta50 = dev_buckets[3]["metrics"]["50"]["hit_rate_delta"]
    q1_q3_chunk_delta50 = mean([
        bucket["metrics"]["50"]["hit_rate_delta"] for bucket in dev_buckets[:3]
    ])
    long_text_conclusion = (
        "Chunked 的收益更集中在最长 Q4"
        if q4_chunk_delta50 > q1_q3_chunk_delta50
        else "没有观察到 Chunked 在最长 Q4 上更明显的 HitRate 收益"
    )

    lines = [
        "# IssueFlow RAG Retrieval Diagnostic",
        "",
        "> 诊断日期：2026-08-16（Asia/Shanghai）  ",
        "> 性质：只诊断、不优化；无付费 LLM/API 调用；TEST 为 post-hoc diagnostic。  ",
        "> 核心口径：一次 Top-100 排名派生全部 K；Recall 是每 Query 相关文档召回率的均值，Candidate Hit Rate 是至少命中一个真值的 Query 比例。",
        "",
        "## 1. Executive Summary",
        "",
        "- 当前按 DEV HitRate@50 → Recall@50 选择的最强 Retriever：`%s`。" % strongest,
        "- 历史 TEST 前冻结的 primary 仍是 `%s`；本次没有回选或改写该决定。" % primary["primary_method"],
        "- 当前线上默认：`hybrid + head512 + top_k=5 + HNSW`，线上环境 Query Prefix 为空；RRF k=60，Reranker 关闭。",
        "- 真实线上 Top-5：DEV Recall/HitRate=%s/%s；TEST post-hoc=%s/%s。" % (
            fmt(dev_online_actual["recall"]["5"]), fmt(dev_online_actual["hit_rate"]["5"]),
            fmt(test_online_actual["recall"]["5"]), fmt(test_online_actual["hit_rate"]["5"]),
        ),
        "- %s" % core_diagnosis,
        "- DEV 到 K=100 %s 90%% Candidate Hit Rate，%s 95%%。" % ("达到" if reaches_90 else "仍未达到", "达到" if reaches_95 else "仍未达到"),
        "- RRF 当前%s：DEV@50 head 分支 rescue/harm=%s/%s，chunk 分支=%s/%s。" % (
            "整体伤害多于救回" if rrf_harmful else "存在净互补或结果混合",
            head_rrf["second_rescue"], head_rrf["second_harm"],
            chunk_rrf["second_rescue"], chunk_rrf["second_harm"],
        ),
        "- Chunked 的 DEV HitRate@50 相对 head512 变化为 %s，P50 延迟倍数为 %s；是否切为线上默认需按该收益/成本权衡。" % (fmt(chunk_delta50), fmt(chunk_ratio, 2)),
        "- 是否直接加 Reranker：%s" % ("可以进入候选池重排验证，但仍应保留 Retriever 失败修复并只在 DEV 决策" if dev_hit50 >= 0.90 else "不应作为第一优先级；先修 Retriever 覆盖，再评估 Reranker"),
        "",
        md_table(
            ["Split", "Method", "Recall@5", "HitRate@5", "Recall@30", "HitRate@30", "Recall@50", "HitRate@50", "Recall@100", "HitRate@100"],
            [
                [
                    "DEV（决策可用）", strongest,
                    fmt(dev_strong["recall"]["5"]), fmt(dev_strong["hit_rate"]["5"]),
                    fmt(dev_strong["recall"]["30"]), fmt(dev_strong["hit_rate"]["30"]),
                    fmt(dev_strong["recall"]["50"]), fmt(dev_strong["hit_rate"]["50"]),
                    fmt(dev_strong["recall"]["100"]), fmt(dev_strong["hit_rate"]["100"]),
                ],
                [
                    "TEST（post-hoc）", strongest,
                    fmt(test_strong["recall"]["5"]), fmt(test_strong["hit_rate"]["5"]),
                    fmt(test_strong["recall"]["30"]), fmt(test_strong["hit_rate"]["30"]),
                    fmt(test_strong["recall"]["50"]), fmt(test_strong["hit_rate"]["50"]),
                    fmt(test_strong["recall"]["100"]), fmt(test_strong["hit_rate"]["100"]),
                ],
                [
                    "DEV online actual/expanded", "current_online_default",
                    fmt(dev_online_actual["recall"]["5"]), fmt(dev_online_actual["hit_rate"]["5"]),
                    fmt(dev_online_curve["recall"]["30"]), fmt(dev_online_curve["hit_rate"]["30"]),
                    fmt(dev_online_curve["recall"]["50"]), fmt(dev_online_curve["hit_rate"]["50"]),
                    fmt(dev_online_curve["recall"]["100"]), fmt(dev_online_curve["hit_rate"]["100"]),
                ],
                [
                    "TEST online actual/expanded", "current_online_default",
                    fmt(test_online_actual["recall"]["5"]), fmt(test_online_actual["hit_rate"]["5"]),
                    fmt(test_online_curve["recall"]["30"]), fmt(test_online_curve["hit_rate"]["30"]),
                    fmt(test_online_curve["recall"]["50"]), fmt(test_online_curve["hit_rate"]["50"]),
                    fmt(test_online_curve["recall"]["100"]), fmt(test_online_curve["hit_rate"]["100"]),
                ],
            ],
        ),
        "",
        "## 2. Environment & Data Integrity",
        "",
        md_table(
            ["Item", "Observed value"],
            [
                ["Initial git commit", raw["source_commit"]],
                ["Current git commit", current_sha],
                ["Branch", branch],
                ["Initial git status", "clean"],
                ["Host Python", args.host_python],
                ["Benchmark Python", raw["execution"]["python"].split("\n")[0]],
                ["Docker", docker_version],
                ["Docker Compose", compose_version],
                ["PostgreSQL", context["postgres_version"]],
                ["Extensions", ", ".join("%s %s" % (item["extname"], item["extversion"]) for item in context["extensions"])],
                ["Alembic", context["alembic_version"]],
            ],
        ),
        "",
        "安全说明：只记录非 secret 配置；没有输出 Token、Webhook 签名、API key、完整环境或数据库连接串。",
        "",
        "仓库约束要求只在 `feat/issueflow-v2` 开发，但本次开始时实际分支为 `%s`。为避免破坏工作区，本次没有切分支、提交或修改 production source；所有新增文件均隔离在 diagnostics 目录。" % branch,
        "",
        "热安全记录：2026-08-16 23:41:49 系统 `mTPL=2`（Heavy）后立即暂停；23:45:33 降为 Moderate，23:47:01 恢复 Nominal。随后 benchmark 与 PostgreSQL 各限制为 0.50 CPU，Query 间冷却 5 秒；Moderate 触发降载、Heavy 触发暂停。",
        "",
        md_table(
            ["Setting", "Effective diagnostic/online value"],
            [
                ["embedding provider", context["settings"]["embedding_provider"]],
                ["embedding model", context["settings"]["embedding_model"]],
                ["embedding dimension", context["settings"]["embedding_dimension"]],
                ["online query prefix", repr(context["settings"]["embedding_query_prefix"])],
                ["frozen benchmark prefix", repr(raw["frozen_config"]["query_prefix"])],
                ["chunk size / overlap / max", "%s / %s / %s" % (context["settings"]["chunk_size"], context["settings"]["chunk_overlap"], context["settings"]["max_chunks"])],
                ["chunk aggregation", raw["frozen_config"]["chunk_aggregation"]],
                ["duplicate_top_k", context["settings"]["duplicate_top_k"]],
                ["duplicate_rrf_k", context["settings"]["duplicate_rrf_k"]],
                ["reranker enabled", context["settings"]["reranker_enabled"]],
                ["model loading", "local cache only during diagnostic"],
            ],
        ),
        "",
    ]

    corpus_rows = []
    for repo in REPOS:
        item = context["corpus"][repo]
        dev_count = sum(qrel["repo"] == repo for qrel in qrels_by_split["dev"])
        test_count = sum(qrel["repo"] == repo for qrel in qrels_by_split["test"])
        corpus_rows.append(
            [repo, item["historical_issue_count"], item["with_head_embedding"], item["missing_head_embedding"], item["actual_chunk_count"], item["missing_chunk_metadata"], dev_count, test_count]
        )
    lines.extend(
        [
            md_table(
                ["Repo", "Historical issues", "With embedding", "Missing embedding", "Chunks", "Issues missing chunks", "DEV queries", "TEST queries"],
                corpus_rows,
            ),
            "",
            "- Corpus total：%s issues；Chunk total：%s。" % (
                sum(context["corpus"][repo]["historical_issue_count"] for repo in REPOS),
                sum(context["corpus"][repo]["actual_chunk_count"] for repo in REPOS),
            ),
            "- Chunk metadata/actual row mismatch：%s。" % context["chunk_count_mismatches"],
            "- 两个 384 维 HNSW 索引均 valid/ready：%s。" % "; ".join(
                "%s (%s, valid=%s, ready=%s)" % (item["index_name"], item["size_pretty"], item["indisvalid"], item["indisready"])
                for item in context["indexes"]
            ),
            "- 全部实际运行 degraded count：%s。" % analysis["degraded_total"],
            "",
            "## 3. Current Production Retrieval Path",
            "",
            "源码路径确认：`backend/app/agents/workflow.py::retrieve_similar_issues` 调用 `HybridRetriever.search(mode=\"hybrid\")`；`backend/app/rag/retrieval.py` 默认 `vector_strategy=\"head512\"`、`exact=False`；`backend/app/core/config.py` 默认 `duplicate_top_k=5`、`duplicate_rrf_k=60`、Reranker 关闭。故真实在线路径为：",
            "",
            "```text",
            "new Issue title/body",
            "  -> lexical branch + BGE head512 vector branch (HNSW)",
            "  -> RRF(k=60)",
            "  -> Top-5",
            "  -> LLM duplicate judgment (human-review suggestion only)",
            "```",
            "",
            "在线环境 Prefix 是空字符串；正式 frozen benchmark Prefix 是 BGE retrieval prefix。两者没有混用：第 13 节单独实测真实在线默认。",
            "",
            "## 4. Dataset / Ground Truth Summary",
            "",
        ]
    )
    qrel_rows = []
    for split in ("dev", "test"):
        for repo in REPOS:
            rows = [qrel for qrel in qrels_by_split[split] if qrel["repo"] == repo]
            qrel_rows.append([
                split.upper(), repo, len(rows), sum(len(row["relevant_issue_numbers"]) for row in rows), sum(len(row["relevant_issue_numbers"]) > 1 for row in rows)
            ])
    dev_clusters = {qrel["cluster_id"] for qrel in qrels_by_split["dev"]}
    test_clusters = {qrel["cluster_id"] for qrel in qrels_by_split["test"]}
    missing_qrels = []
    time_violations = []
    for qrel in all_qrels.values():
        query_item = catalog.get((qrel["repo"], qrel["query_issue_number"]))
        if query_item is None:
            missing_qrels.append(qrel["id"] + " query")
        for number in qrel["relevant_issue_numbers"]:
            target = catalog.get((qrel["repo"], number))
            if target is None:
                missing_qrels.append("%s target #%s" % (qrel["id"], number))
            elif query_item and target["github_created_at"] >= query_item["github_created_at"]:
                time_violations.append("%s -> #%s" % (qrel["id"], number))
    candidate_missing = candidate_self = candidate_time_violations = candidate_count = 0
    candidate_groups = []
    for split_runs in raw["runs"].values():
        for phase_runs in split_runs.values():
            candidate_groups.extend(phase_runs.values())
    candidate_groups.extend(online["runs"].values())
    candidate_groups.extend(online["configured_top5_runs"].values())
    for group in candidate_groups:
        for record in group:
            if "ranked" not in record:
                continue
            query_time = datetime.fromisoformat(record["query_created_at"].replace("Z", "+00:00"))
            for number in record["ranked"]:
                candidate_count += 1
                if number == record["query_issue_number"]:
                    candidate_self += 1
                candidate = catalog.get((record["repo"], number))
                if candidate is None:
                    candidate_missing += 1
                    continue
                candidate_time = datetime.fromisoformat(candidate["github_created_at"].replace("Z", "+00:00"))
                if candidate_time >= query_time:
                    candidate_time_violations += 1
    lines.extend(
        [
            md_table(["Split", "Repo", "Queries", "Relevant links", "Multi-relevant queries"], qrel_rows),
            "",
            "- DEV 74、TEST 90，共 164 Query；Duplicate clusters=%s。" % len(clusters["clusters"]),
            "- DEV/TEST cluster overlap=%s；数据库缺失的 Query/真值=%s；真值时间边界违规=%s。" % (len(dev_clusters & test_clusters), len(missing_qrels), len(time_violations)),
            "- 本次已落盘候选 %s 个；self-match=%s、数据库缺失=%s、候选时间边界违规=%s。" % (candidate_count, candidate_self, candidate_missing, candidate_time_violations),
            "- 当前数据库与 snapshot stored_issue_count 差异：%s。" % "; ".join(
                "%s=%+d" % (repo, context["corpus"][repo]["historical_issue_count"] - snapshots["repos"][repo]["stored_issue_count"])
                for repo in REPOS
            ),
            "- Dataset SHA-256：DEV `%s`；TEST `%s`。" % (raw["dataset_hashes"]["dev"], raw["dataset_hashes"]["test"]),
            "- 候选规则始终为同仓库、非自身、`candidate.created_at < query.created_at`；排名输入只有 title/body。",
            "",
            "## 5. Full Retrieval Results",
            "",
            "DEV 可用于下一阶段工程选择；TEST 只用于 post-hoc diagnosis。以下数值均来自本次 Top-100 实跑。",
            "",
        ]
    )
    append_metric_tables(lines, analysis, "dev")
    append_metric_tables(lines, analysis, "test")

    lines.extend(["### 达到 80% / 90% / 95% 的最小已测 K", ""])
    threshold_rows = []
    for split in ("dev", "test"):
        for method in METHODS:
            summary = metrics[split][method]["overall"]
            threshold_rows.append([
                split.upper(), method,
                threshold_k(summary, "hit_rate", 0.80), threshold_k(summary, "hit_rate", 0.90), threshold_k(summary, "hit_rate", 0.95),
                threshold_k(summary, "recall", 0.80), threshold_k(summary, "recall", 0.90), threshold_k(summary, "recall", 0.95),
            ])
    lines.extend([
        md_table(["Split", "Method", "Hit≥80%", "Hit≥90%", "Hit≥95%", "Recall≥80%", "Recall≥90%", "Recall≥95%"], threshold_rows),
        "",
        "`未达到` 表示到 K=100 仍未达到；这里只报告已测 K，不在相邻 K 之间插值。",
        "",
        "## 6. Candidate Hit Rate Curves",
        "",
    ])
    for method in KEY_METHODS:
        rows = []
        for k in K_VALUES:
            rows.append([
                k,
                fmt(metrics["dev"][method]["overall"]["recall"][str(k)]),
                fmt(metrics["dev"][method]["overall"]["hit_rate"][str(k)]),
                fmt(metrics["test"][method]["overall"]["recall"][str(k)]),
                fmt(metrics["test"][method]["overall"]["hit_rate"][str(k)]),
            ])
        lines.extend([
            "### `%s`" % method,
            "",
            md_table(["K", "DEV Recall", "DEV Candidate Hit Rate", "TEST Recall (post-hoc)", "TEST Candidate Hit Rate (post-hoc)"], rows),
            "",
        ])

    lines.extend(["## 7. Per-Repository Results", ""])
    for split in ("dev", "test"):
        for repo in REPOS:
            rows = []
            for method in METHODS:
                summary = metrics[split][method]["per_repo"][repo]
                rows.append(
                    [method]
                    + [fmt(summary["recall"][str(k)]) for k in K_VALUES]
                    + [fmt(summary["hit_rate"][str(k)]) for k in K_VALUES]
                )
            lines.extend([
                "### %s — `%s`" % (split.upper(), repo),
                "",
                md_table(["Method"] + ["R@%s" % k for k in K_VALUES] + ["H@%s" % k for k in K_VALUES], rows),
                "",
            ])

    lines.extend(["## 8. First Relevant Rank Distribution", ""])
    for split in ("dev", "test"):
        rows = []
        for method in METHODS:
            item = analysis["first_relevant_rank"][split][method]
            rates = item["rates"]
            percentiles = item["percentiles_among_found"]
            rows.append([
                method, pct(rates["rank_1"]), pct(rates["le_5"]), pct(rates["le_10"]), pct(rates["le_20"]), pct(rates["le_30"]), pct(rates["le_50"]), pct(rates["le_100"]), pct(rates["gt_100_or_not_found"]),
                fmt(percentiles["median"], 1), fmt(percentiles["p75"], 1), fmt(percentiles["p90"], 1), fmt(percentiles["p95"], 1),
            ])
        lines.extend([
            "### %s" % split.upper(),
            "",
            md_table(["Method", "rank=1", "≤5", "≤10", "≤20", "≤30", "≤50", "≤100", ">100/miss", "Median*", "P75*", "P90*", "P95*"], rows),
            "",
            "\* 分位数只在 Top-100 内找到真值的 Query 上计算；未找到比例单独报告，避免把删失值伪装成精确 rank。",
            "",
        ])

    lines.extend(["## 9. Head512 vs Chunked", ""])
    for split in ("dev", "test"):
        pair_rows = []
        for k in K_VALUES:
            pair = analysis["head512_vs_chunked"][split]["pair_by_k"][str(k)]
            pair_rows.append([
                k,
                fmt(metrics[split]["vector_head512"]["overall"]["recall"][str(k)]),
                fmt(metrics[split]["vector_chunked"]["overall"]["recall"][str(k)]),
                fmt(metrics[split]["vector_head512"]["overall"]["hit_rate"][str(k)]),
                fmt(metrics[split]["vector_chunked"]["overall"]["hit_rate"][str(k)]),
                pair["second_rescue"], pair["second_harm"], pair["both_hit"], pair["both_miss"],
            ])
        movement = analysis["head512_vs_chunked"][split]["rank_movement_chunk_minus_head"]
        lines.extend([
            "### %s Query-level 对比" % split.upper(),
            "",
            md_table(["K", "Head Recall", "Chunk Recall", "Head HitRate", "Chunk HitRate", "Chunk rescued", "Chunk harmed", "Both hit", "Both miss"], pair_rows),
            "",
            "First Relevant Rank：Chunk 上升 %s、下降 %s、不变 %s；从 >100 救回 %s、反向丢失 %s。" % (
                movement["second_rank_improved"], movement["second_rank_declined"], movement["unchanged"], movement["rescued_from_beyond_100"], movement["lost_beyond_100"]
            ),
            "",
        ])
    lines.extend(["### Query 长度 Quartile", ""])
    bucket_rows = []
    for split in ("dev", "test"):
        for bucket in analysis["head512_vs_chunked"][split]["length_buckets"]:
            for k in (30, 50, 100):
                item = bucket["metrics"][str(k)]
                bucket_rows.append([
                    split.upper(), bucket["bucket"], "%s–%s" % (bucket["min_tokens"], bucket["max_tokens"]), k,
                    fmt(item["head_hit_rate"]), fmt(item["chunk_hit_rate"]), fmt(item["hit_rate_delta"]),
                    fmt(item["head_recall"]), fmt(item["chunk_recall"]), fmt(item["recall_delta"]),
                    item["pair"]["second_rescue"], item["pair"]["second_harm"],
                ])
    lines.extend([
        md_table(["Split", "Token quartile", "Token range", "K", "Head H", "Chunk H", "ΔH", "Head R", "Chunk R", "ΔR", "rescued", "harmed"], bucket_rows),
        "",
        "Matched Exact latency（同一 0.50 CPU 探针）：DEV head/chunk P50=%s/%s ms（%.2fx），P95=%s/%s ms；TEST P50=%s/%s ms，P95=%s/%s ms。" % (
            ms(analysis["matched_ann_latency"]["dev"]["vector_head512"]["exact_p50_latency_ms"]),
            ms(analysis["matched_ann_latency"]["dev"]["vector_chunked"]["exact_p50_latency_ms"]), chunk_ratio,
            ms(analysis["matched_ann_latency"]["dev"]["vector_head512"]["exact_p95_latency_ms"]),
            ms(analysis["matched_ann_latency"]["dev"]["vector_chunked"]["exact_p95_latency_ms"]),
            ms(analysis["matched_ann_latency"]["test"]["vector_head512"]["exact_p50_latency_ms"]),
            ms(analysis["matched_ann_latency"]["test"]["vector_chunked"]["exact_p50_latency_ms"]),
            ms(analysis["matched_ann_latency"]["test"]["vector_head512"]["exact_p95_latency_ms"]),
            ms(analysis["matched_ann_latency"]["test"]["vector_chunked"]["exact_p95_latency_ms"]),
        ),
        "%s：DEV@50 Q4 ΔHitRate=%s，Q1–Q3 平均 ΔHitRate=%s。" % (
            long_text_conclusion, fmt(q4_chunk_delta50), fmt(q1_q3_chunk_delta50)
        ),
        "",
    ])

    combined_head = by_id([
        {**record, "_split": split} for split in ("dev", "test") for record in raw["runs"][split]["exact"]["vector_head512"] if "ranked" in record
    ])
    combined_chunk = by_id([
        {**record, "_split": split} for split in ("dev", "test") for record in raw["runs"][split]["exact"]["vector_chunked"] if "ranked" in record
    ])
    chunk_rescued = [combined_chunk[case_id] for case_id in combined_head if not combined_head[case_id]["hit"]["30"] and combined_chunk[case_id]["hit"]["30"]]
    head_rescued = [combined_head[case_id] for case_id in combined_head if combined_head[case_id]["hit"]["30"] and not combined_chunk[case_id]["hit"]["30"]]
    for records_list, title in ((chunk_rescued[:3], "Chunk rescued head miss @30"), (head_rescued[:3], "Head512 rescued chunk miss @30")):
        lines.extend([
            "### %s 代表案例" % title,
            "",
            md_table(["Repo", "Query", "Ground Truth", "First rank", "Top wrong", "Observed fact", "Hypothesis"], case_table_rows(records_list, all_qrels, catalog)),
            "",
        ])

    lines.extend(["## 10. Lexical vs Vector Complementarity", ""])
    lexical_dev = metrics["dev"]["lexical"]["overall"]
    vector_dev = metrics["dev"]["vector_head512"]["overall"]
    lines.extend([
        "事实：lexical 在 DEV K=30/50/100 的 Recall=%s/%s/%s、HitRate=%s/%s/%s；同期 vector_head512 Recall=%s/%s/%s、HitRate=%s/%s/%s。lexical 到 K=100 %s追上。" % (
            fmt(lexical_dev["recall"]["30"]), fmt(lexical_dev["recall"]["50"]), fmt(lexical_dev["recall"]["100"]),
            fmt(lexical_dev["hit_rate"]["30"]), fmt(lexical_dev["hit_rate"]["50"]), fmt(lexical_dev["hit_rate"]["100"]),
            fmt(vector_dev["recall"]["30"]), fmt(vector_dev["recall"]["50"]), fmt(vector_dev["recall"]["100"]),
            fmt(vector_dev["hit_rate"]["30"]), fmt(vector_dev["hit_rate"]["50"]), fmt(vector_dev["hit_rate"]["100"]),
            "仍未" if lexical_dev["hit_rate"]["100"] < vector_dev["hit_rate"]["100"] else "已经",
        ),
        "",
    ])
    complement_rows = []
    for split in ("dev", "test"):
        for vector_method in ("vector_head512", "vector_chunked"):
            for k in (10, 30, 50):
                item = analysis["lexical_vs_vector"][split][vector_method][str(k)]
                complement_rows.append([
                    split.upper(), vector_method, k,
                    item["second_rescue"], item["second_harm"], item["both_hit"], item["both_miss"],
                ])
    lines.extend([
        md_table(["Split", "Vector comparator", "K", "Lexical-only", "Vector-only", "Both hit", "Both miss"], complement_rows),
        "",
        "这里 `second_rescue=Lexical-only`，`second_harm=Vector-only`。事实结论与原因推测分开：集合计数是事实；下面的语义/噪声解释只是基于标题、正文长度与错误文本的诊断假设。",
        "",
    ])
    # Deterministic representative TEST samples at K=30 against head512.
    lex_test = by_id(raw["runs"]["test"]["exact"]["lexical"])
    vec_test = by_id(raw["runs"]["test"]["exact"]["vector_head512"])
    categories = {
        "Vector-only success": [vec_test[case_id] for case_id in vec_test if vec_test[case_id]["hit"]["30"] and not lex_test[case_id]["hit"]["30"]],
        "Lexical-only success": [lex_test[case_id] for case_id in vec_test if not vec_test[case_id]["hit"]["30"] and lex_test[case_id]["hit"]["30"]],
        "Both fail": [vec_test[case_id] for case_id in vec_test if not vec_test[case_id]["hit"]["30"] and not lex_test[case_id]["hit"]["30"]],
    }
    for label, sample_records in categories.items():
        sample = sorted(sample_records, key=lambda record: (-record["query_token_count"], record["id"]))[:3]
        lines.extend([
            "### %s（TEST post-hoc, K=30）" % label,
            "",
            md_table(["Repo", "Query", "Ground Truth", "First rank", "Top wrong", "Observed fact", "Hypothesis"], case_table_rows(sample, all_qrels, catalog)),
            "",
        ])

    lines.extend(["## 11. RRF Rescue / Harm Analysis", ""])
    rrf_rows = []
    for split in ("dev", "test"):
        for hybrid in ("hybrid_head512_rrf", "hybrid_chunked_rrf"):
            for k in (5, 10, 30, 50):
                item = analysis["rrf"][split][hybrid]["pair_by_k"][str(k)]
                rrf_rows.append([
                    split.upper(), hybrid, k, item["second_rescue"], item["second_harm"], item["both_hit"], item["both_miss"]
                ])
    lines.extend([
        md_table(["Split", "Hybrid", "K", "Hybrid rescue", "Hybrid harm", "Both hit", "Both miss"], rrf_rows),
        "",
    ])
    movement_rows = []
    for split in ("dev", "test"):
        for hybrid in ("hybrid_head512_rrf", "hybrid_chunked_rrf"):
            item = analysis["rrf"][split][hybrid]["rank_movement"]
            movement_rows.append([
                split.upper(), hybrid, item["second_rank_improved"], item["second_rank_declined"], item["unchanged"], item["rescued_from_beyond_100"], item["lost_beyond_100"], fmt(item["median_second_minus_first_rank_capped_101"], 1)
            ])
    lines.extend([
        md_table(["Split", "Hybrid", "Rank up", "Rank down", "Unchanged", "Rescued >100", "Lost >100", "Median Δrank*"], movement_rows),
        "",
        "DEV@50 事实：head vector→hybrid 的 HitRate %s→%s、Recall %s→%s、rescue/harm=%s/%s；chunk vector→hybrid 的 HitRate %s→%s、Recall %s→%s、rescue/harm=%s/%s。" % (
            fmt(metrics["dev"]["vector_head512"]["overall"]["hit_rate"]["50"]),
            fmt(metrics["dev"]["hybrid_head512_rrf"]["overall"]["hit_rate"]["50"]),
            fmt(metrics["dev"]["vector_head512"]["overall"]["recall"]["50"]),
            fmt(metrics["dev"]["hybrid_head512_rrf"]["overall"]["recall"]["50"]),
            head_rrf["second_rescue"], head_rrf["second_harm"],
            fmt(metrics["dev"]["vector_chunked"]["overall"]["hit_rate"]["50"]),
            fmt(metrics["dev"]["hybrid_chunked_rrf"]["overall"]["hit_rate"]["50"]),
            fmt(metrics["dev"]["vector_chunked"]["overall"]["recall"]["50"]),
            fmt(metrics["dev"]["hybrid_chunked_rrf"]["overall"]["recall"]["50"]),
            chunk_rrf["second_rescue"], chunk_rrf["second_harm"],
        ),
        "",
        "\* Δrank = Hybrid − Vector，负数为上升；未找到按 101 仅用于方向计数。若 harm/下降持续多于 rescue/上升，就属于情况 C，不能假设 Hybrid 必然更好。",
        "",
        "## 12. Exact vs HNSW",
        "",
    ])
    hnsw_rows = []
    for split in ("dev", "test"):
        for method in ("vector_head512", "vector_chunked"):
            for k in (10, 30, 50):
                item = analysis["exact_vs_hnsw"][split][method][str(k)]
                matched_latency = analysis["matched_ann_latency"][split][method]
                hnsw_rows.append([
                    split.upper(), method, k,
                    fmt(item["hnsw_top_k_recall_relative_to_exact"]),
                    fmt(item["exact_order_match_rate"]),
                    fmt(item["exact_hit_rate"]), fmt(item["hnsw_hit_rate"]), fmt(item["hit_rate_delta_hnsw_minus_exact"]),
                    ms(matched_latency["exact_p50_latency_ms"]), ms(matched_latency["hnsw_p50_latency_ms"]), ms(matched_latency["exact_p95_latency_ms"]), ms(matched_latency["hnsw_p95_latency_ms"]),
                ])
    lines.extend([
        md_table(["Split", "Method", "K", "HNSW recall vs Exact", "Exact order match", "Exact HitRate", "HNSW HitRate", "ΔHit", "Exact P50", "HNSW P50", "Exact P95", "HNSW P95"], hnsw_rows),
        "",
        "Exact/HNSW 排名与 HitRate 使用全量 Query；延迟来自同一 0.5-CPU、5 秒 cooldown 下、按正文长度分层的匹配样本（每 split×repo 5 条，共 30 Query，并交替 Exact/HNSW 顺序）。延迟仍包含当前 `HybridRetriever.search` 的完整路径；即使 mode=vector，现实现也会先执行 lexical SQL 再丢弃 lexical 结果，因此不是纯 ANN kernel microbenchmark。",
        "",
        "## 13. Current Online Default: Effect of Increasing K Only",
        "",
        "**CURRENT ONLINE DEFAULT**：`mode=hybrid`、`vector_strategy=head512`、`top_k=5`、`exact=False`（HNSW）、空 Query Prefix、RRF k=60、Reranker disabled。由于当前代码把 branch fetch depth 设为 `3 × top_k`，真实 Top-5 另跑一次；K≥10 使用一次 Top-100 完整排名切片。",
        "",
    ])
    online_rows = []
    for split in ("dev", "test"):
        actual = analysis["online_configured_top5_metrics"][split]["overall"]
        expanded = analysis["online_metrics"][split]["overall"]
        for k in (1, 5):
            online_rows.append([split.upper(), "actual top_k=5", k, fmt(actual["recall"][str(k)]), fmt(actual["hit_rate"][str(k)]), actual["hit_count"][str(k)], actual["query_count"]])
        for k in K_VALUES:
            online_rows.append([split.upper(), "Top-100 slice", k, fmt(expanded["recall"][str(k)]), fmt(expanded["hit_rate"][str(k)]), expanded["hit_count"][str(k)], expanded["query_count"]])
    lines.extend([
        md_table(["Split", "Run", "K", "Recall", "Candidate Hit Rate", "Hit queries", "Queries"], online_rows),
        "",
        "在线默认阈值：",
        "",
        md_table(
            ["Split", "Hit≥80%", "Hit≥90%", "Hit≥95%", "Recall≥80%", "Recall≥90%", "Recall≥95%"],
            [
                [split.upper()] + [
                    threshold_k(analysis["online_effective_curve"][split], metric, threshold)
                    for metric in ("hit_rate", "recall")
                    for threshold in (0.80, 0.90, 0.95)
                ]
                for split in ("dev", "test")
            ],
        ),
        "",
        "## 14. Failure Case Analysis",
        "",
    ])
    strongest_records = [
        {**record, "_split": split}
        for split in ("dev", "test")
        for record in raw["runs"][split]["exact"][strongest]
        if "ranked" in record
    ]
    failures = [record for record in strongest_records if not record["hit"]["30"]]
    selected_failures = balanced_select(
        failures,
        10,
        lambda record: (
            not record["hit"]["50"],
            record["first_relevant_rank"] is None,
            record["first_relevant_rank"] or 101,
            record["query_token_count"],
        ),
    )
    lines.extend([
        "最强方法 `%s` 中优先选 Top-30 miss，再按 Top-50 miss、first rank 与长度做三仓库平衡抽样。" % strongest,
        "",
        md_table(["Split", "Repo", "Query", "Ground Truth", "First rank", "Top wrong candidates", "Observed facts", "Possible reason (hypothesis)"], case_table_rows(selected_failures, all_qrels, catalog, include_split=True)),
        "",
        "## 15. Successful Case Analysis",
        "",
    ])
    successes = [record for record in strongest_records if record["first_relevant_rank"] == 1]
    selected_successes = balanced_select(
        successes, 5, lambda record: (-record["query_token_count"], record["id"])
    )
    lines.extend([
        md_table(["Split", "Repo", "Query", "Ground Truth", "First rank", "Top wrong candidates", "Observed facts", "Possible reason (hypothesis)"], case_table_rows(selected_successes, all_qrels, catalog, include_split=True)),
        "",
        "成功案例用于对照系统擅长的模式；原因仍是诊断假设，不是因果证明。",
        "",
        "## 16. Latency / Cost Trade-offs",
        "",
    ])
    latency_rows = []
    for split in ("dev", "test"):
        for method in METHODS:
            item = metrics[split][method]["overall"]
            latency_rows.append([split.upper(), "Exact raw*", method, ms(item["p50_latency_ms"]), ms(item["p95_latency_ms"]), item["min_returned_candidates"], fmt(item["median_returned_candidates"], 1)])
        for method in ("vector_head512", "vector_chunked"):
            hnsw_records = raw["runs"][split]["hnsw"][method]
            item = summarize(hnsw_records)
            latency_rows.append([split.upper(), "HNSW raw*", method, ms(item["p50_latency_ms"]), ms(item["p95_latency_ms"]), item["min_returned_candidates"], fmt(item["median_returned_candidates"], 1)])
            matched = analysis["matched_ann_latency"][split][method]
            latency_rows.append([split.upper(), "Exact matched", method, ms(matched["exact_p50_latency_ms"]), ms(matched["exact_p95_latency_ms"]), "sample n=%s" % matched["query_count"], "—"])
            latency_rows.append([split.upper(), "HNSW matched", method, ms(matched["hnsw_p50_latency_ms"]), ms(matched["hnsw_p95_latency_ms"]), "sample n=%s" % matched["query_count"], "—"])
        online_item = analysis["online_metrics"][split]["overall"]
        actual_top5 = analysis["online_configured_top5_metrics"][split]["overall"]
        latency_rows.append([split.upper(), "HNSW", "current_online_default expanded Top-100", ms(online_item["p50_latency_ms"]), ms(online_item["p95_latency_ms"]), online_item["min_returned_candidates"], fmt(online_item["median_returned_candidates"], 1)])
        latency_rows.append([split.upper(), "HNSW", "current_online_default actual Top-5", ms(actual_top5["p50_latency_ms"]), ms(actual_top5["p95_latency_ms"]), actual_top5["min_returned_candidates"], fmt(actual_top5["median_returned_candidates"], 1)])
    lines.extend([
        md_table(["Split", "Search", "Method", "P50 ms", "P95 ms", "Min returned", "Median returned"], latency_rows),
        "",
        "\* 用户观察到系统热压力 Heavy 后，任务立即暂停并改为 0.5 CPU + 每 Query 5 秒 cooldown；因此 full-run raw latency 跨越不同资源阶段，只作运行记录，不用于 Exact/HNSW 公平结论。公平 ANN latency 采用 matched rows。排名与 Recall/HitRate 不受 CPU 配额影响。",
        "",
        "费用：$0 外部模型/API；FastEmbed 仅从既有 Docker volume 本地加载，PostgreSQL/pgvector 在本机运行。这里的 latency 是串行单 Query wall-clock，不代表线上并发吞吐。",
        "",
        "## 17. Test / Ruff Results",
        "",
    ])
    test_rows = []
    for item in verification.get("commands", []):
        test_rows.append([
            item["command"], item["exit_code"], item.get("passed", "—"), item.get("failed", "—"), item.get("skipped", "—"), item.get("notes", "")
        ])
    lines.extend([
        md_table(["Command", "Exit", "Passed", "Failed", "Skipped", "Notes"], test_rows),
        "",
        "未为通过测试而删除、改写或放宽任何旧测试。",
        "",
        "## 18. Limitations",
        "",
        "- **已有 TEST 是 post-hoc diagnostic，仅用于诊断，不再是新的未见过的 unbiased held-out test。** TEST 结果没有用于改 Prefix、Chunk 聚合、split、qrels、clusters 或 frozen config。",
        "- 下一阶段工程决策应优先看 DEV；TEST 只能作为已观察数据的解释性证据。",
        "- 这是 maintainer-derived positive retrieval benchmark，不是完整 duplicate classification；没有代表性负样本 Precision/F1。",
        "- 候选语料是 2026-08-06 的时间对齐快照（6,485 issues），不代表三个仓库全部历史。",
        "- Top-100 之外只记作未找到，无法知道真实 rank 是 101 还是更深。",
        "- RRF Top-100 内部每个分支取 300；扩大 K 同时扩大了分支深度，因此这里准确反映当前实现，但不是固定 branch-depth 的纯 K 截断实验。",
        "- Exact/HNSW latency 受 Docker、缓存、串行顺序和当前 `vector` 模式仍执行 lexical SQL 的实现影响。",
        "- 为保护 MacBook Air 电池，运行中从无限制负载切换为 0.5 CPU + 5 秒 cooldown；不同阶段的 raw latency 不可直接横比，第 12 节使用独立匹配样本。",
        "- 没有运行或实现 Reranker；不能从这些数据推断实际 Reranker 的排序质量。",
        "",
        "## 19. Decision for Next Step",
        "",
    ])
    decisions = []
    if dev_hit50 >= 0.90:
        decisions.append("**P1 — A. 先验证 Reranker**：强 Retriever 在 DEV Top-50 Candidate Hit Rate 已达到 90%；用 Top-30/50 高召回池重排到 Top-5 有数据依据。")
    else:
        decisions.append("**P1 — B. 先优化 Retriever**：强 Retriever 在 DEV Top-50 Candidate Hit Rate 仍低于 90%；即使完美 Reranker 也无法救回未进入候选池的 Query。")
    if rrf_harmful:
        decisions.append("**P2 — C. 先修 Hybrid/RRF**：两个 Hybrid 在 DEV@50 的 harm 均多于 rescue；融合不是免费增益，需在 DEV 上重新设计后再考虑默认。")
    else:
        decisions.append("**P2 — C. 审核 Hybrid/RRF**：融合有互补，但仍需依据 rescue/harm 与 rank movement 判断是否值得保留。")
    if chunk_ratio is not None and chunk_ratio >= 1.30 and chunk_delta50 <= 0.03:
        decisions.append("**P3 — D. 暂不把 Chunked 切为线上默认**：HitRate@50 净收益有限而 P50 延迟明显增加；先保留为候选实验分支。")
    else:
        decisions.append("**P3 — D. 按长度路由评估 Chunked**：结合 quartile 收益和延迟决定是否只服务长 Query，不直接全量切换。")
    lines.extend(decisions)
    lines.extend([
        "",
        "对应情形判断：A=%s，B=%s，C=%s，D=%s。" % (
            "是" if dev_hit50 >= 0.90 else "否",
            "是" if dev_hit50 < 0.90 else "否",
            "是" if rrf_harmful else "部分/待审",
            "是" if chunk_ratio is not None and chunk_ratio >= 1.30 and chunk_delta50 <= 0.03 else "条件性",
        ),
        "",
        "## 20. Exact Reproduction Commands",
        "",
        "```bash",
        "cd /Users/cy/Code/github项目/issueflow-agent",
        "docker compose up -d --no-deps postgres",
        "",
        "# Frozen DEV/TEST Top-100: Exact all 5 methods + HNSW vector methods",
        "docker compose -f docker-compose.yml -f eval/reports/diagnostics/docker-compose.thermal-safe.yml run --rm --no-deps \\",
        "  -v \"$PWD/backend:/app:ro\" -v \"$PWD/eval:/eval:ro\" \\",
        "  -v \"$PWD/eval/reports/diagnostics:/diagnostics\" \\",
        "  -e PYTHONPATH=/app -e EMBEDDING_LOCAL_FILES_ONLY=true backend \\",
        "  python /diagnostics/run_top100_diagnostic.py \\",
        "  --dev-qrels /eval/datasets/duplicate_qrels_dev.jsonl \\",
        "  --test-qrels /eval/datasets/duplicate_qrels_test.jsonl \\",
        "  --frozen-config /eval/reports/rag_frozen_config.json \\",
        "  --output /diagnostics/rag_retrieval_top100_raw_2026-08-16.json \\",
        "  --source-commit %s --cooldown-seconds 5" % raw["source_commit"],
        "",
        "# Actual online default with K widened only",
        "docker compose -f docker-compose.yml -f eval/reports/diagnostics/docker-compose.thermal-safe.yml run --rm --no-deps \\",
        "  -v \"$PWD/backend:/app:ro\" -v \"$PWD/eval:/eval:ro\" \\",
        "  -v \"$PWD/eval/reports/diagnostics:/diagnostics\" \\",
        "  -e PYTHONPATH=/app -e EMBEDDING_LOCAL_FILES_ONLY=true backend \\",
        "  python /diagnostics/run_online_default_top100.py \\",
        "  --dev-qrels /eval/datasets/duplicate_qrels_dev.jsonl \\",
        "  --test-qrels /eval/datasets/duplicate_qrels_test.jsonl \\",
        "  --output /diagnostics/rag_current_online_default_top100_raw_2026-08-16.json \\",
        "  --source-commit %s --cooldown-seconds 5" % raw["source_commit"],
        "",
        "# Matched Exact/HNSW latency probe (30 stratified queries)",
        "docker compose -f docker-compose.yml -f eval/reports/diagnostics/docker-compose.thermal-safe.yml run --rm --no-deps \\",
        "  -v \"$PWD/backend:/app:ro\" -v \"$PWD/eval:/eval:ro\" \\",
        "  -v \"$PWD/eval/reports/diagnostics:/diagnostics\" \\",
        "  -e PYTHONPATH=/app -e EMBEDDING_LOCAL_FILES_ONLY=true backend \\",
        "  python /diagnostics/run_matched_ann_latency_probe.py \\",
        "  --dev-qrels /eval/datasets/duplicate_qrels_dev.jsonl \\",
        "  --test-qrels /eval/datasets/duplicate_qrels_test.jsonl \\",
        "  --frozen-config /eval/reports/rag_frozen_config.json \\",
        "  --output /diagnostics/rag_ann_matched_latency_2026-08-17.json \\",
        "  --source-commit %s --per-repo 5 --cooldown-seconds 5" % raw["source_commit"],
        "",
        "# Read-only environment/corpus/catalog capture",
        "docker compose -f docker-compose.yml -f eval/reports/diagnostics/docker-compose.thermal-safe.yml run --rm --no-deps \\",
        "  -v \"$PWD/backend:/app:ro\" \\",
        "  -v \"$PWD/eval/reports/diagnostics:/diagnostics\" \\",
        "  -e PYTHONPATH=/app -e EMBEDDING_LOCAL_FILES_ONLY=true backend \\",
        "  python /diagnostics/collect_diagnostic_context.py \\",
        "  --output /diagnostics/rag_diagnostic_context_2026-08-17.json",
        "",
        "# Build JSON/CSV and the Desktop Markdown",
        "python3 eval/reports/diagnostics/build_retrieval_diagnostic_report.py \\",
        "  --repo-root \"$PWD\" \\",
        "  --raw \"$PWD/eval/reports/diagnostics/rag_retrieval_top100_raw_2026-08-16.json\" \\",
        "  --online-raw \"$PWD/eval/reports/diagnostics/rag_current_online_default_top100_raw_2026-08-16.json\" \\",
        "  --ann-probe \"$PWD/eval/reports/diagnostics/rag_ann_matched_latency_2026-08-17.json\" \\",
        "  --context \"$PWD/eval/reports/diagnostics/rag_diagnostic_context_2026-08-17.json\" \\",
        "  --dev-qrels \"$PWD/eval/datasets/duplicate_qrels_dev.jsonl\" \\",
        "  --test-qrels \"$PWD/eval/datasets/duplicate_qrels_test.jsonl\" \\",
        "  --snapshots \"$PWD/eval/datasets/repository_snapshots.json\" \\",
        "  --clusters \"$PWD/eval/datasets/duplicate_clusters.json\" \\",
        "  --primary \"$PWD/eval/reports/rag_primary_method.json\" \\",
        "  --verification \"$PWD/eval/reports/diagnostics/verification_results_2026-08-17.json\" \\",
        "  --output-analysis \"$PWD/eval/reports/diagnostics/rag_retrieval_top100_analysis_2026-08-17.json\" \\",
        "  --output-csv \"$PWD/eval/reports/diagnostics/rag_retrieval_top100_metrics_2026-08-17.csv\" \\",
        "  --output-markdown \"/Users/cy/Desktop/issueflow_rag_retrieval_diagnostic_2026-08-16.md\" \\",
        "  --host-python \"Python 3.9.6\"",
        "```",
        "",
        "检索脚本是可断点续跑的；已存在且 dataset hash/commit 一致的记录会跳过。测试/Ruff 的逐条命令、退出码与计数见第 17 节。",
        "",
        "## 21. Files Changed / Temporary Files",
        "",
        "- Production source changed：**No**。没有修改 `backend/app`、production defaults、qrels、clusters、split、frozen config 或历史正式报告。",
        "- 新增/生成的仓库文件仅位于 `eval/reports/diagnostics/`：诊断脚本、raw JSON、analysis JSON、metrics CSV、context JSON、verification JSON。",
        "- Desktop deliverable：`%s`。" % args.output_markdown,
        "- Docker side effects：启动 Docker Desktop 以恢复既有 volume；自动恢复的旧 GitHub collector、Worker、Backend 已立即停止；没有删除容器/volume。收尾时已停止本次所需容器并恢复 Docker 初始关闭状态。",
        "- Git status command exit=%s；最终可见状态：" % git_rc,
        "",
        "```text",
        git_status or "clean",
        "```",
        "",
        "Raw artifacts:",
        "",
        "- `%s`" % args.raw,
        "- `%s`" % args.online_raw,
        "- `%s`" % args.ann_probe,
        "- `%s`" % args.output_analysis,
        "- `%s`" % args.output_csv,
        "- `%s`" % args.context,
        "",
    ])
    return "\n".join(lines)


def write_metrics_csv(path: Path, analysis: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["split", "scope", "repo", "method", "k", "recall", "candidate_hit_rate", "hit_count", "query_count", "p50_latency_ms", "p95_latency_ms"])
        for split in ("dev", "test", "combined"):
            for method in METHODS:
                grouped = analysis["metrics"][split][method]
                scopes = [("overall", "", grouped["overall"]), ("macro_average", "", grouped["macro_average"])]
                scopes.extend(("per_repo", repo, grouped["per_repo"][repo]) for repo in REPOS)
                for scope, repo, summary in scopes:
                    for k in K_VALUES:
                        writer.writerow([
                            split, scope, repo, method, k,
                            summary["recall"][str(k)], summary["hit_rate"][str(k)],
                            summary.get("hit_count", {}).get(str(k), ""), summary["query_count"],
                            summary.get("p50_latency_ms", ""), summary.get("p95_latency_ms", ""),
                        ])
        for split in ("dev", "test", "combined"):
            summary = analysis["online_metrics"][split]["overall"]
            for k in K_VALUES:
                writer.writerow([
                    split, "overall", "", "current_online_default", k,
                    summary["recall"][str(k)], summary["hit_rate"][str(k)], summary["hit_count"][str(k)], summary["query_count"], summary["p50_latency_ms"], summary["p95_latency_ms"],
                ])
        for split in ("dev", "test"):
            summary = analysis["online_configured_top5_metrics"][split]["overall"]
            for k in (1, 5):
                writer.writerow([
                    split, "overall", "", "current_online_default_actual_top5", k,
                    summary["recall"][str(k)], summary["hit_rate"][str(k)], summary["hit_count"][str(k)], summary["query_count"], summary["p50_latency_ms"], summary["p95_latency_ms"],
                ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--online-raw", type=Path, required=True)
    parser.add_argument("--ann-probe", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--dev-qrels", type=Path, required=True)
    parser.add_argument("--test-qrels", type=Path, required=True)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--clusters", type=Path, required=True)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--output-analysis", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--host-python", default=sys.version.split("\n")[0])
    args = parser.parse_args()

    raw = load_json(args.raw)
    online = load_json(args.online_raw)
    ann_probe = load_json(args.ann_probe)
    context = load_json(args.context)
    snapshots = load_json(args.snapshots)
    clusters = load_json(args.clusters)
    primary = load_json(args.primary)
    verification = load_json(args.verification)
    qrels_by_split = {
        "dev": load_jsonl(args.dev_qrels),
        "test": load_jsonl(args.test_qrels),
    }
    expected = {"dev": 74, "test": 90}
    errors = list(raw["execution"].get("errors", [])) + list(online["execution"].get("errors", [])) + list(ann_probe["execution"].get("errors", []))
    if errors:
        raise SystemExit("Retrieval artifact contains errors: %s" % errors[:3])
    for split, count in expected.items():
        for method in METHODS:
            if len(raw["runs"][split]["exact"].get(method, [])) != count:
                raise SystemExit("Incomplete exact records for %s/%s" % (split, method))
        for method in ("vector_head512", "vector_chunked"):
            if len(raw["runs"][split]["hnsw"].get(method, [])) != count:
                raise SystemExit("Incomplete HNSW records for %s/%s" % (split, method))
        if len(online["runs"].get(split, [])) != count:
            raise SystemExit("Incomplete online records for %s" % split)
        if len(online["configured_top5_runs"].get(split, [])) != count:
            raise SystemExit("Incomplete configured Top-5 records for %s" % split)

    expected_probe = ann_probe["sample"]["per_repo_per_split"] * len(REPOS)
    for split in ("dev", "test"):
        for method in ("vector_head512", "vector_chunked"):
            for search_type in ("exact", "hnsw"):
                if len(ann_probe["runs"][split][method][search_type]) != expected_probe:
                    raise SystemExit("Incomplete matched ANN probe for %s/%s/%s" % (split, method, search_type))

    analysis = build_analysis(raw, online, ann_probe, context, qrels_by_split)
    args.output_analysis.parent.mkdir(parents=True, exist_ok=True)
    args.output_analysis.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_metrics_csv(args.output_csv, analysis)
    report = render_report(
        args, raw, online, context, snapshots, clusters, primary, verification,
        analysis, qrels_by_split,
    )
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(report + "\n", encoding="utf-8")
    print(json.dumps({
        "strongest_method": analysis["strongest_method_by_dev_hit50_then_recall50"],
        "analysis": str(args.output_analysis),
        "csv": str(args.output_csv),
        "markdown": str(args.output_markdown),
        "markdown_bytes": args.output_markdown.stat().st_size,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
