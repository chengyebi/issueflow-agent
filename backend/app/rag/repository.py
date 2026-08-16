import re
from dataclasses import dataclass
from datetime import datetime

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.db.connection import connect
from app.rag.embedding import EmbeddingObservation
from app.rag.schema import HistoricalIssue, SearchHit
from app.rag.text import ISSUE_EMBEDDING_TEXT_VERSION, issue_content_hash

# Pathological issue text (markdown table separators, code blocks with long runs of
# '-', or simply very long bodies) can make websearch_to_tsquery blow its internal
# parser stack ("tsquery stack too small"). The tsquery input is bounded and runs of
# the '-' exclusion operator are collapsed; similarity()/%% still score the full query.
LEXICAL_TSQUERY_MAX_CHARS = 2000
LEXICAL_TSQUERY_DASH_RUN = re.compile(r"-{2,}")


@dataclass(frozen=True)
class UpsertResult:
    historical_issue_id: int
    content_hash: str
    created: bool
    content_changed: bool
    embedding_needed: bool
    embedding_model: str | None
    embedding_dimensions: int | None
    embedding_text_version: str | None


def upsert_historical_issue(cur, issue: HistoricalIssue) -> UpsertResult:
    content_hash = issue_content_hash(issue.title, issue.body, issue.labels)
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
            github_created_at, github_updated_at, content_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (repo, issue_number) DO UPDATE SET
            title = EXCLUDED.title,
            body = EXCLUDED.body,
            labels = EXCLUDED.labels,
            state = EXCLUDED.state,
            github_created_at = EXCLUDED.github_created_at,
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
            embedding_text_version = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_text_version END,
            embedding_input_characters = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_input_characters END,
            embedding_original_tokens = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_original_tokens END,
            embedding_embedded_tokens = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_embedded_tokens END,
            embedding_max_tokens = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_max_tokens END,
            embedding_truncated = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedding_truncated END,
            embedded_at = CASE
                WHEN historical_issues.content_hash <> EXCLUDED.content_hash
                THEN NULL ELSE historical_issues.embedded_at END,
            updated_at = NOW()
        RETURNING id, content_hash,
                  (embedding_content_hash IS NULL OR embedding IS NULL)
                      AS embedding_needed,
                  embedding_model, embedding_dimensions, embedding_text_version
        """,
        (
            issue.repo,
            issue.issue_number,
            issue.title,
            issue.body,
            Jsonb(issue.labels),
            issue.state,
            issue.github_created_at,
            issue.github_updated_at,
            content_hash,
        ),
    )
    row = cur.fetchone()
    content_changed = old_hash is None or old_hash != content_hash
    if content_changed:
        cur.execute(
            "DELETE FROM historical_issue_chunks WHERE historical_issue_id = %s",
            (row[0],),
        )
        cur.execute(
            """
            UPDATE historical_issues SET
                chunk_strategy_version = NULL, chunk_embedding_model = NULL,
                tokenizer_name = NULL, chunk_size = NULL, chunk_overlap = NULL,
                chunk_original_token_count = NULL, chunk_stored_token_count = NULL,
                chunk_truncated_token_count = NULL, chunk_count = NULL
            WHERE id = %s
            """,
            (row[0],),
        )
    return UpsertResult(
        historical_issue_id=row[0],
        content_hash=str(row[1]).strip(),
        created=existing is None,
        content_changed=content_changed,
        embedding_needed=bool(row[2]),
        embedding_model=row[3],
        embedding_dimensions=row[4],
        embedding_text_version=row[5],
    )


class PostgresHistoricalIssueRepository:
    def upsert(self, issue: HistoricalIssue) -> UpsertResult:
        with connect() as conn, conn.cursor() as cur:
            return upsert_historical_issue(cur, issue)

    def get_for_embedding(self, historical_issue_id: int) -> dict | None:
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, repo, issue_number, title, body, labels, content_hash,
                       embedding_model, embedding_dimensions, embedding_content_hash,
                       embedding_text_version
                FROM historical_issues WHERE id = %s
                """,
                (historical_issue_id,),
            )
            return cur.fetchone()

    def list_for_indexing(self, repo: str, limit: int) -> list[dict]:
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, repo, issue_number, title, body, labels, content_hash,
                       embedding_model, embedding_dimensions, embedding_content_hash,
                       embedding_text_version, chunk_strategy_version,
                       chunk_embedding_model, tokenizer_name, chunk_size,
                       chunk_overlap, chunk_count
                FROM historical_issues
                WHERE repo = %s
                ORDER BY github_created_at DESC, issue_number DESC
                LIMIT %s
                """,
                (repo, limit),
            )
            return list(cur.fetchall())

    def save_embedding(
        self,
        historical_issue_id: int,
        content_hash: str,
        model_name: str,
        dimensions: int,
        vector: list[float],
        *,
        text_version: str = ISSUE_EMBEDDING_TEXT_VERSION,
        observation: EmbeddingObservation | None = None,
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
                    embedding_text_version = %s,
                    embedding_input_characters = %s,
                    embedding_original_tokens = %s,
                    embedding_embedded_tokens = %s,
                    embedding_max_tokens = %s,
                    embedding_truncated = %s,
                    embedded_at = NOW(), updated_at = NOW()
                WHERE id = %s AND content_hash = %s
                  AND (
                      embedding_content_hash IS DISTINCT FROM %s
                      OR embedding_model IS DISTINCT FROM %s
                      OR embedding_dimensions IS DISTINCT FROM %s
                      OR embedding_text_version IS DISTINCT FROM %s
                  )
                RETURNING id
                """,
                (
                    vector_literal,
                    model_name,
                    dimensions,
                    content_hash,
                    text_version,
                    observation.input_characters if observation else None,
                    observation.original_tokens if observation else None,
                    observation.embedded_tokens if observation else None,
                    observation.max_input_tokens if observation else None,
                    observation.truncated if observation else None,
                    historical_issue_id,
                    content_hash,
                    content_hash,
                    model_name,
                    dimensions,
                    text_version,
                ),
            )
            return cur.fetchone() is not None

    def lexical_search(
        self,
        repo: str,
        query: str,
        limit: int,
        exclude_issue_number: int | None = None,
        created_before: datetime | None = None,
    ) -> list[SearchHit]:
        tsquery_input = LEXICAL_TSQUERY_DASH_RUN.sub(
            " ", query[:LEXICAL_TSQUERY_MAX_CHARS]
        )
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id AS historical_issue_id, repo, issue_number, title, body, state,
                       github_created_at,
                       GREATEST(
                           similarity(title, %s),
                           similarity(body, %s),
                           ts_rank_cd(search_vector, websearch_to_tsquery('simple', %s))
                       ) AS score
                FROM historical_issues
                WHERE repo = %s
                  AND (%s::integer IS NULL OR issue_number <> %s::integer)
                  AND (%s::timestamptz IS NULL OR github_created_at < %s::timestamptz)
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
                    tsquery_input,
                    repo,
                    exclude_issue_number,
                    exclude_issue_number,
                    created_before,
                    created_before,
                    query,
                    query,
                    tsquery_input,
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
        created_before: datetime | None = None,
        *,
        exact: bool = False,
    ) -> list[SearchHit]:
        if len(query_vector) != dimensions or not 2 <= dimensions <= 4096:
            raise ValueError("查询向量维度无效")
        vector_literal = "[" + ",".join(f"{value:.12g}" for value in query_vector) + "]"
        embedding_cast = sql.SQL("embedding::vector({})").format(sql.Literal(dimensions))
        query = sql.SQL(
            """
            SELECT id AS historical_issue_id, repo, issue_number, title, body, state,
                   github_created_at,
                   1 - ({embedding} <=> %s::vector({dimensions})) AS score
            FROM historical_issues
            WHERE repo = %s AND embedding IS NOT NULL
              AND embedding_model = %s AND embedding_dimensions = %s
              AND (%s::integer IS NULL OR issue_number <> %s::integer)
              AND (%s::timestamptz IS NULL OR github_created_at < %s::timestamptz)
            ORDER BY {embedding} <=> %s::vector({dimensions}), issue_number DESC
            LIMIT %s
            """
        ).format(embedding=embedding_cast, dimensions=sql.Literal(dimensions))
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            if exact:
                cur.execute("SET LOCAL enable_indexscan = off")
                cur.execute("SET LOCAL enable_bitmapscan = off")
            else:
                cur.execute("SET LOCAL enable_seqscan = off")
                cur.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            cur.execute(
                query,
                (
                    vector_literal,
                    repo,
                    model_name,
                    dimensions,
                    exclude_issue_number,
                    exclude_issue_number,
                    created_before,
                    created_before,
                    vector_literal,
                    limit,
                ),
            )
            return [SearchHit.model_validate(row) for row in cur.fetchall()]

    def chunk_state(self, historical_issue_id: int) -> dict | None:
        with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, body, content_hash, chunk_strategy_version,
                       chunk_embedding_model, tokenizer_name, chunk_size,
                       chunk_overlap, chunk_count
                FROM historical_issues WHERE id = %s
                """,
                (historical_issue_id,),
            )
            return cur.fetchone()

    def save_chunks(
        self,
        historical_issue_id: int,
        *,
        expected_content_hash: str,
        chunks: list,
        vectors: list[list[float]],
        strategy_version: str,
        model_name: str,
        tokenizer_name: str,
        chunk_size: int,
        chunk_overlap: int,
        original_token_count: int,
        stored_token_count: int,
        truncated_token_count: int,
    ) -> bool:
        if len(chunks) != len(vectors) or any(len(vector) != 384 for vector in vectors):
            raise ValueError("Chunk 与 384 维向量数量或维度不一致")
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT content_hash FROM historical_issues WHERE id = %s FOR UPDATE",
                (historical_issue_id,),
            )
            row = cur.fetchone()
            if row is None or str(row[0]).strip() != expected_content_hash:
                return False
            cur.execute(
                "DELETE FROM historical_issue_chunks WHERE historical_issue_id = %s",
                (historical_issue_id,),
            )
            for chunk, vector in zip(chunks, vectors, strict=True):
                vector_literal = "[" + ",".join(f"{value:.12g}" for value in vector) + "]"
                cur.execute(
                    """
                    INSERT INTO historical_issue_chunks (
                        historical_issue_id, chunk_index, chunk_type, chunk_text,
                        token_count, content_hash, embedding, chunk_strategy_version,
                        embedding_model, tokenizer_name, chunk_size, chunk_overlap
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::vector(384), %s, %s, %s, %s, %s)
                    """,
                    (
                        historical_issue_id, chunk.index, chunk.chunk_type, chunk.text,
                        chunk.token_count, chunk.content_hash, vector_literal,
                        strategy_version, model_name, tokenizer_name, chunk_size,
                        chunk_overlap,
                    ),
                )
            cur.execute(
                """
                UPDATE historical_issues SET
                    chunk_strategy_version = %s, chunk_embedding_model = %s,
                    tokenizer_name = %s, chunk_size = %s, chunk_overlap = %s,
                    chunk_original_token_count = %s, chunk_stored_token_count = %s,
                    chunk_truncated_token_count = %s, chunk_count = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    strategy_version, model_name, tokenizer_name, chunk_size,
                    chunk_overlap, original_token_count, stored_token_count,
                    truncated_token_count, len(chunks), historical_issue_id,
                ),
            )
        return True

    def chunk_vector_search(
        self,
        repo: str,
        query_vectors: list[list[float]],
        model_name: str,
        limit: int,
        exclude_issue_number: int | None = None,
        created_before: datetime | None = None,
        *,
        aggregation: str = "max_chunk_score",
        exact: bool = False,
    ) -> list[SearchHit]:
        if not query_vectors or any(len(vector) != 384 for vector in query_vectors):
            raise ValueError("Chunk 查询必须包含 384 维向量")
        if aggregation not in {"max_chunk_score", "mean_top2_chunk_score"}:
            raise ValueError("不支持的 Chunk 聚合策略")
        scored: dict[int, list[SearchHit]] = {}
        for query_vector in query_vectors:
            vector_literal = "[" + ",".join(f"{value:.12g}" for value in query_vector) + "]"
            with connect(row_factory=dict_row) as conn, conn.cursor() as cur:
                if exact:
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute("SET LOCAL enable_bitmapscan = off")
                else:
                    cur.execute("SET LOCAL enable_seqscan = off")
                    cur.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
                cur.execute(
                    """
                    SELECT hi.id AS historical_issue_id, hi.repo, hi.issue_number,
                           hi.title, hi.body, hi.state, hi.github_created_at,
                           hic.chunk_index AS best_chunk_index,
                           1 - (hic.embedding <=> %s::vector(384)) AS score
                    FROM historical_issue_chunks hic
                    JOIN historical_issues hi ON hi.id = hic.historical_issue_id
                    WHERE hi.repo = %s AND hic.embedding_model = %s
                      AND (%s::integer IS NULL OR hi.issue_number <> %s::integer)
                      AND (%s::timestamptz IS NULL OR hi.github_created_at < %s::timestamptz)
                    ORDER BY hic.embedding <=> %s::vector(384), hi.issue_number DESC
                    LIMIT %s
                    """,
                    (
                        vector_literal, repo, model_name, exclude_issue_number,
                        exclude_issue_number, created_before, created_before,
                        vector_literal, max(limit * 8, 100),
                    ),
                )
                for row in cur.fetchall():
                    hit = SearchHit.model_validate(row)
                    scored.setdefault(hit.historical_issue_id, []).append(hit)
        aggregated = []
        for hits in scored.values():
            scores = sorted((hit.score for hit in hits), reverse=True)
            score = scores[0] if aggregation == "max_chunk_score" else sum(scores[:2]) / min(2, len(scores))
            aggregated.append(hits[0].model_copy(update={"score": score}))
        return sorted(aggregated, key=lambda hit: (-hit.score, -hit.issue_number))[:limit]
