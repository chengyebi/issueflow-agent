"""增加 Issue 创建时间与 Token 感知 Chunk 向量。"""

from alembic import op

revision = "0005_issue_chunk_retrieval"
down_revision = "0004_fastembed_384"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE historical_issues
            ADD COLUMN IF NOT EXISTS github_created_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS chunk_strategy_version TEXT,
            ADD COLUMN IF NOT EXISTS chunk_embedding_model TEXT,
            ADD COLUMN IF NOT EXISTS tokenizer_name TEXT,
            ADD COLUMN IF NOT EXISTS chunk_size INTEGER,
            ADD COLUMN IF NOT EXISTS chunk_overlap INTEGER,
            ADD COLUMN IF NOT EXISTS chunk_original_token_count INTEGER,
            ADD COLUMN IF NOT EXISTS chunk_stored_token_count INTEGER,
            ADD COLUMN IF NOT EXISTS chunk_truncated_token_count INTEGER,
            ADD COLUMN IF NOT EXISTS chunk_count INTEGER;

        UPDATE historical_issues
        SET github_created_at = COALESCE(github_created_at, github_updated_at)
        WHERE github_created_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_historical_issues_repo_created
            ON historical_issues (repo, github_created_at, issue_number);

        CREATE TABLE IF NOT EXISTS historical_issue_chunks (
            id BIGSERIAL PRIMARY KEY,
            historical_issue_id BIGINT NOT NULL
                REFERENCES historical_issues(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
            chunk_type TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            token_count INTEGER NOT NULL CHECK (token_count > 0),
            content_hash CHAR(64) NOT NULL,
            embedding VECTOR(384) NOT NULL,
            chunk_strategy_version TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            tokenizer_name TEXT NOT NULL,
            chunk_size INTEGER NOT NULL,
            chunk_overlap INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (
                historical_issue_id, chunk_index, chunk_strategy_version,
                embedding_model, chunk_size, chunk_overlap
            )
        );
        CREATE INDEX IF NOT EXISTS idx_historical_issue_chunks_issue
            ON historical_issue_chunks (historical_issue_id, chunk_index);
        CREATE INDEX IF NOT EXISTS idx_historical_issue_chunks_hnsw_384
            ON historical_issue_chunks USING hnsw (embedding vector_cosine_ops);
        """
    )


def downgrade() -> None:
    # Chunk data and source timestamps are intentionally retained.
    pass
