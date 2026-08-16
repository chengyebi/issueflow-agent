"""自动化可观测指标聚合。

从 automation_decisions 与 agent_runs 聚合：
  auto_execute_rate / defer_rate / no_action_rate
  human_touch_rate
  defer_reason_distribution
  llm_calls_per_100_issues / llm_cost_per_100_issues（来自 agent_runs token/cost）
"""

from psycopg.rows import dict_row

from app.db.connection import connect


def aggregate_automation_metrics() -> dict:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE disposition = 'auto_execute') AS auto_execute_count,
                COUNT(*) FILTER (WHERE disposition = 'defer') AS defer_count,
                COUNT(*) FILTER (WHERE disposition = 'no_action') AS no_action_count
            FROM automation_decisions
            """
        )
        row = cur.fetchone()
        total = row["total"] or 0

        cur.execute(
            """
            SELECT reason_code, COUNT(*) AS n
            FROM automation_decisions
            WHERE disposition = 'defer'
            GROUP BY reason_code
            ORDER BY n DESC
            """
        )
        defer_reasons = {
            r["reason_code"] or "unknown": r["n"] for r in cur.fetchall()
        }

        cur.execute(
            """
            SELECT
                SUM(input_tokens) AS total_input,
                SUM(output_tokens) AS total_output,
                SUM(estimated_cost_usd) AS total_cost_usd,
                COUNT(*) AS runs
            FROM agent_runs
            WHERE status = 'completed'
            """
        )
        cost_row = cur.fetchone()
        runs = cost_row["runs"] or 0
        total_cost = float(cost_row["total_cost_usd"] or 0.0)

    auto = row["auto_execute_count"] or 0
    defer = row["defer_count"] or 0
    no_action = row["no_action_count"] or 0

    llm_calls_per_100 = runs / max(1, total) * 100 if total else 0.0
    cost_per_100 = total_cost / max(1, runs) * 100 if runs else 0.0

    return {
        "sample_count": total,
        "auto_execute_rate": round(auto / total, 4) if total else 0.0,
        "defer_rate": round(defer / total, 4) if total else 0.0,
        "no_action_rate": round(no_action / total, 4) if total else 0.0,
        "human_touch_rate": round((defer + no_action) / total, 4) if total else 0.0,
        "defer_reason_distribution": defer_reasons,
        "llm_calls_per_100_issues": round(llm_calls_per_100, 2),
        "llm_cost_per_100_issues": round(cost_per_100, 4),
    }
