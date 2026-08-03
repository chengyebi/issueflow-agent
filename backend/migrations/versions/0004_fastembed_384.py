"""增加 384 维向量索引与 Embedding 输入可观测字段。"""

from alembic import op

revision = "0004_fastembed_384"
down_revision = "0003_historical_issue_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE historical_issues
            ADD COLUMN IF NOT EXISTS embedding_text_version TEXT,
            ADD COLUMN IF NOT EXISTS embedding_input_characters INTEGER,
            ADD COLUMN IF NOT EXISTS embedding_original_tokens INTEGER,
            ADD COLUMN IF NOT EXISTS embedding_embedded_tokens INTEGER,
            ADD COLUMN IF NOT EXISTS embedding_max_tokens INTEGER,
            ADD COLUMN IF NOT EXISTS embedding_truncated BOOLEAN;

        CREATE INDEX IF NOT EXISTS idx_historical_issues_embedding_hnsw_384
            ON historical_issues USING hnsw (
                (embedding::vector(384)) vector_cosine_ops
            )
            WHERE embedding_dimensions = 384;
        """
    )


def downgrade() -> None:
    # Existing vectors and observability metadata are intentionally retained.
    pass
