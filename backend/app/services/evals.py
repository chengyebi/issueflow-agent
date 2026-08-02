from psycopg.rows import dict_row

from app.db.connection import connect


def list_eval_reports(limit: int = 50) -> list[dict]:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT eval_id, dataset_name, dataset_hash, runner_type, model_name,
                   prompt_version, agent_version, agent_mode, case_count,
                   metrics, publishable_model_score, created_at
            FROM eval_reports ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


def get_eval_report(eval_id: str) -> dict | None:
    with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT eval_id, dataset_name, dataset_hash, runner_type, model_name,
                   prompt_version, agent_version, agent_mode, case_count,
                   metrics, report_json, publishable_model_score, created_at
            FROM eval_reports WHERE eval_id = %s
            """,
            (eval_id,),
        )
        return cur.fetchone()
