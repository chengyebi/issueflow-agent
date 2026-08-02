from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.db.connection import connect


class DatabaseTraceRecorder:
    def __init__(self, agent_run_id: int, trace_id: str):
        self.agent_run_id = agent_run_id
        self.trace_id = trace_id
        self.sequence = 0

    def start_node(self, node_name: str, input_summary: dict) -> int:
        self.sequence += 1
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_node_traces (
                    trace_id, agent_run_id, node_name, sequence_number,
                    status, input_summary
                ) VALUES (%s, %s, %s, %s, 'running', %s)
                RETURNING id
                """,
                (
                    self.trace_id,
                    self.agent_run_id,
                    node_name,
                    self.sequence,
                    Jsonb(input_summary),
                ),
            )
            return cur.fetchone()[0]

    def finish_node(
        self,
        span_id: int,
        duration_ms: int,
        output_summary: dict,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_node_traces
                SET status = 'completed', finished_at = NOW(), duration_ms = %s,
                    output_summary = %s, input_tokens = %s, output_tokens = %s
                WHERE id = %s
                """,
                (
                    duration_ms,
                    Jsonb(output_summary),
                    input_tokens,
                    output_tokens,
                    span_id,
                ),
            )

    def fail_node(
        self,
        span_id: int,
        duration_ms: int,
        error_type: str,
        error_message: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agent_node_traces
                SET status = 'failed', finished_at = NOW(), duration_ms = %s,
                    error_type = %s, error_message = %s,
                    input_tokens = %s, output_tokens = %s
                WHERE id = %s
                """,
                (
                    duration_ms,
                    error_type,
                    error_message,
                    input_tokens,
                    output_tokens,
                    span_id,
                ),
            )


def list_traces(
    status: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    query = """
        SELECT ar.trace_id, ar.id AS agent_run_id, ar.status, ar.model_name,
               ar.prompt_version, ar.agent_version, ar.agent_mode,
               ar.duration_ms, ar.input_tokens, ar.output_tokens, ar.retry_count,
               ar.structured_output_success, ar.error_type, ar.error_message,
               ar.estimated_cost_usd, ar.created_at, ar.started_at, ar.finished_at,
               ie.repo, ie.issue_number, ie.issue_title
        FROM agent_runs ar JOIN issue_events ie ON ie.id = ar.issue_event_id
    """
    params: list = []
    if status:
        query += " WHERE ar.status = %s"
        params.append(status)
    query += " ORDER BY ar.id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def get_trace(trace_id: str) -> dict | None:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.trace_id, ar.id AS agent_run_id, ar.status, ar.model_name,
                   ar.prompt_version, ar.agent_version, ar.agent_mode,
                   ar.duration_ms, ar.input_tokens, ar.output_tokens, ar.retry_count,
                   ar.structured_output_success, ar.error_type, ar.error_message,
                   ar.estimated_cost_usd, ar.result_json,
                   ar.created_at, ar.started_at, ar.finished_at,
                   ie.repo, ie.issue_number, ie.issue_title
            FROM agent_runs ar JOIN issue_events ie ON ie.id = ar.issue_event_id
            WHERE ar.trace_id = %s
            """,
            (trace_id,),
        )
        run = cur.fetchone()
        if run is None:
            return None
        cur.execute(
            """
            SELECT id, node_name, sequence_number, status, started_at, finished_at,
                   duration_ms, input_summary, output_summary,
                   input_tokens, output_tokens, retry_count, error_type, error_message
            FROM agent_node_traces WHERE trace_id = %s ORDER BY sequence_number
            """,
            (trace_id,),
        )
        run["nodes"] = list(cur.fetchall())
        return run


def aggregate_run_metrics() -> dict:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS total_runs,
                   COUNT(*) FILTER (WHERE status = 'completed') AS completed_runs,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed_runs,
                   AVG(input_tokens + output_tokens) FILTER (
                       WHERE status = 'completed' AND prompt_version IS NOT NULL
                   )
                       AS average_tokens,
                   AVG(estimated_cost_usd) FILTER (WHERE status = 'completed')
                       AS average_estimated_cost_usd,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       FILTER (WHERE status = 'completed') AS duration_p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                       FILTER (WHERE status = 'completed') AS duration_p95_ms,
                   AVG(CASE WHEN structured_output_success THEN 1.0 ELSE 0.0 END)
                       FILTER (WHERE structured_output_success IS NOT NULL)
                       AS structured_output_success_rate
            FROM agent_runs
            """
        )
        result = dict(cur.fetchone())
        total = result["total_runs"] or 0
        result["agent_success_rate"] = (
            result["completed_runs"] / total if total else None
        )
        return result
