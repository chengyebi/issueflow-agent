import hashlib
import json
from dataclasses import dataclass

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.db.connection import connect
from app.rag.schema import HistoricalIssue, SearchHit


@dataclass(frozen=True)
class UpsertResult:
    historical_issue_id: int
    content_hash: str
    created: bool
    content_changed: bool
    embedding_needed: bool
    embedding_model: str | None
    embedding_dimensions: int | None


def issue_content_hash(title: str, body: str) -> str:
    canonical = json.dumps(
        {"title": title.strip(), "body": body.strip()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def upsert_historical_issue(cur, issue: HistoricalIssue) -> UpsertResult:
    content_hash = issue_content_hash(issue.title, issue.body)
    cur.execute(
        """
        SELECT content_hash
        FROM historical_issues
        WHERE repo = %s AND issue_number = %s
        FOR UPDATE
        """,
        (issue.repo, issue.issue_number),
    )
    existing = cur.fetchone()
    old_hash = str(existing[0]).strip() if existing is not None else None
    cur.execute(
        """
        INSERT INTO historical_issues (
            repo, issue_number, title, body, labels, state,
            github_updated_at, content_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo, issue_number) DO UPDATE SET
            title = EXCLUDED.title,
            body = EXCLUDED.body,
            labels = EXCLUDED.labels,
            state = EXCLUDED.state,
            github_updated_at = EXCLUDED.github_updated_at,
            content_hash = EXCLUDED.content_hash,
            indexed_at = NOW(),
            embedding = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding END,
            embedding_model = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_model END,
            embedding_dimensions = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_dimensions END,
            embedding_content_hash = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_content_hash END,
            embedded_at = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedded_at END,
            updated_at = NOW()
        RETURNING id, content_hash,
                  (embedding_content_hash IS NULL OR embedding IS NULL)
                      AS embedding_needed,
                  embedding_model, embedding_dimensions
        """,
        (
            issue.repo,
            issue.issue_number,
            issue.title,
            issue.body,
            Jsonb(issue.labels),
            issue.state,
            issue.github_updated_at,
            content_hash,
        ),
    )
    row = cur.fetchone()
    return UpsertResult(
        historical_issue_id=row[0],
        content_hash=str(row[1]).strip(),
        created=existing is None,
        content_changed=old_hash is None or old_hash != content_hash,
        embedding_needed=bool(row[2]),
        embedding_model=row[3],
        embedding_dimensions=row[4],
    )


class PostgresHistoricalIssueRepository:
    def upsert(self, issue: HistoricalIssue) -> UpsertResult:
        with connect() as conn, conn.cursor() as cur:
            return upsert_historical_issue(cur, issue)

    def get_for_embedding(self, historical_issue_id: int) -> dict | None:
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, repo, issue_number, title, body, content_hash,
                       embedding_model, embedding_dimensions, embedding_content_hash
                FROM historical_issues WHERE id = %s
                """,
                (historical_issue_id,),
            )
            return cur.fetchone()

    def save_embedding(
        self,
        historical_issue_id: int,
        content_hash: str,
        model_name: str,
        dimensions: int,
        vector: list[float],
    ) -> bool:
        if len(vector) != dimensions:
            raise ValueError("Embedding 向量维度与 Provider 配置不一致")
        vector_literal = "[" + ",".join(f"{value:.12g}" for value in vector) + "]"
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE historical_issues
                SET embedding = %s::vector,
                    embedding_model = %s,
                    embedding_dimensions = %s,
                    embedding_content_hash = %s,
                    embedded_at = NOW(), updated_at = NOW()
                WHERE id = %s AND content_hash = %s
                  AND (
                      embedding_content_hash IS DISTINCT FROM %s
                      OR embedding_model IS DISTINCT FROM %s
                      OR embedding_dimensions IS DISTINCT FROM %s
                  )
                RETURNING id
                """,
                (
                    vector_literal,
                    model_name,
                    dimensions,
                    content_hash,
                    historical_issue_id,
                    content_hash,
                    content_hash,
                    model_name,
                    dimensions,
                ),
            )
            return cur.fetchone() is not None

    def lexical_search(
        self, repo: str, query: str, limit: int, exclude_issue_number: int | None = None
    ) -> list[SearchHit]:
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id AS historical_issue_id, repo, issue_number, title, body, state,
                       GREATEST(
                           similarity(title, %s),
                           similarity(body, %s),
                           ts_rank_cd(search_vector, websearch_to_tsquery('simple', %s))
                       ) AS score
                FROM historical_issues
                WHERE repo = %s
                  AND (%s::integer IS NULL OR issue_number <> %s::integer)
                  AND (
                      title %% %s OR body %% %s
                      OR search_vector @@ websearch_to_tsquery('simple', %s)
                  )
                ORDER BY score DESC, issue_number DESC
                LIMIT %s
                """,
                (
                    query,
                    query,
                    query,
                    repo,
                    exclude_issue_number,
                    exclude_issue_number,
                    query,
                    query,
                    query,
                    limit,
                ),
            )
            return [SearchHit.model_validate(row) for row in cur.fetchall()]

    def vector_search(
        self,
        repo: str,
        query_vector: list[float],
        model_name: str,
        dimensions: int,
        limit: int,
        exclude_issue_number: int | None = None,
    ) -> list[SearchHit]:
        if len(query_vector) != dimensions or not 2 <= dimensions <= 4096:
            raise ValueError("查询向量维度无效")
        vector_literal = "[" + ",".join(f"{value:.12g}" for value in query_vector) + "]"
        embedding_cast = sql.SQL("embedding::vector({})").format(sql.Literal(dimensions))
        query = sql.SQL(
            """
            SELECT id AS historical_issue_id, repo, issue_number, title, body, state,
                   1 - ({embedding} <=> %s::vector({dimensions})) AS score
            FROM historical_issues
            WHERE repo = %s AND embedding IS NOT NULL
              AND embedding_model = %s AND embedding_dimensions = %s
              AND (%s::integer IS NULL OR issue_number <> %s::integer)
            ORDER BY {embedding} <=> %s::vector({dimensions}), issue_number DESC
            LIMIT %s
            """
        ).format(embedding=embedding_cast, dimensions=sql.Literal(dimensions))
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                query,
                (
                    vector_literal,
                    repo,
                    model_name,
                    dimensions,
                    exclude_issue_number,
                    exclude_issue_number,
                    vector_literal,
                    limit,
                ),
            )
            return [SearchHit.model_validate(row) for row in cur.fetchall()]
