"""增加历史 Issue、向量索引、同步记录与查重结果。"""

from alembic import op

revision = "0003_historical_issue_rag"
down_revision = "0002_trace_eval_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS pg_trgm;

        CREATE TABLE IF NOT EXISTS historical_issues (
            id BIGSERIAL PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            labels JSONB NOT NULL DEFAULT '[]'::jsonb,
            state TEXT NOT NULL CHECK (state IN ('open', 'closed')),
            github_updated_at TIMESTAMPTZ NOT NULL,
            content_hash CHAR(64) NOT NULL,
            indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            embedding VECTOR,
            embedding_model TEXT,
            embedding_dimensions INTEGER,
            embedding_content_hash CHAR(64),
            embedded_at TIMESTAMPTZ,
            search_vector TSVECTOR GENERATED ALWAYS AS (
                to_tsvector(
                    'simple',
                    coalesce(title, '') || ' ' || coalesce(body, '')
                )
            ) STORED,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (repo, issue_number)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_issues_repo_state
            ON historical_issues (repo, state, issue_number DESC);
        CREATE INDEX IF NOT EXISTS idx_historical_issues_search_vector
            ON historical_issues USING GIN (search_vector);
        CREATE INDEX IF NOT EXISTS idx_historical_issues_title_trgm
            ON historical_issues USING GIN (title gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_historical_issues_body_trgm
            ON historical_issues USING GIN (body gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_historical_issues_embedding_hnsw_16
            ON historical_issues USING hnsw (
                (embedding::vector(16)) vector_cosine_ops
            )
            WHERE embedding_dimensions = 16;

        CREATE TABLE IF NOT EXISTS issue_sync_runs (
            id BIGSERIAL PRIMARY KEY,
            repo TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            scanned_count INTEGER NOT NULL DEFAULT 0,
            upserted_count INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            embedded_count INTEGER NOT NULL DEFAULT 0,
            skipped_pull_request_count INTEGER NOT NULL DEFAULT 0,
            error_type TEXT,
            error_message TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_issue_sync_runs_repo
            ON issue_sync_runs (repo, id DESC);

        CREATE TABLE IF NOT EXISTS duplicate_assessments (
            id BIGSERIAL PRIMARY KEY,
            agent_run_id INTEGER NOT NULL UNIQUE
                REFERENCES agent_runs(id) ON DELETE CASCADE,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            is_duplicate BOOLEAN NOT NULL,
            candidate_issue_number INTEGER,
            confidence DOUBLE PRECISION NOT NULL CHECK (
                confidence >= 0.0 AND confidence <= 1.0
            ),
            rationale TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            retrieval_mode TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_duplicate_assessments_repo
            ON duplicate_assessments (repo, issue_number);

        ALTER TABLE outbox_events
            DROP CONSTRAINT IF EXISTS outbox_events_event_type_check;
        ALTER TABLE outbox_events
            ADD CONSTRAINT outbox_events_event_type_check
            CHECK (event_type IN ('agent_run', 'review_commands', 'issue_index'));
        """
    )


def downgrade() -> None:
    # Historical indexes and assessment evidence are retained intentionally.
    pass
