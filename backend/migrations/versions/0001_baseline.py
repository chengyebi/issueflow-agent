"""接管现有业务表并建立迁移基线。"""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id SERIAL PRIMARY KEY,
            delivery_id TEXT NOT NULL UNIQUE,
            event_name TEXT NOT NULL,
            raw_payload JSONB NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS issue_events (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            repo TEXT NOT NULL,
            action TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            issue_body TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            webhook_delivery_id INTEGER REFERENCES webhook_deliveries(id)
        );
        ALTER TABLE issue_events
            ADD COLUMN IF NOT EXISTS webhook_delivery_id INTEGER
            REFERENCES webhook_deliveries(id);
        CREATE INDEX IF NOT EXISTS idx_issue_events_webhook_delivery_id
            ON issue_events (webhook_delivery_id);

        CREATE TABLE IF NOT EXISTS agent_runs (
            id SERIAL PRIMARY KEY,
            issue_event_id INTEGER NOT NULL UNIQUE REFERENCES issue_events(id),
            status TEXT NOT NULL DEFAULT 'pending',
            rq_job_id TEXT,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            error_message TEXT,
            result_json JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS result_json JSONB;
        CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs (status);

        CREATE TABLE IF NOT EXISTS review_tasks (
            id SERIAL PRIMARY KEY,
            agent_run_id INTEGER NOT NULL UNIQUE REFERENCES agent_runs(id),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
            reviewer TEXT,
            review_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks (status);

        CREATE TABLE IF NOT EXISTS github_commands (
            id SERIAL PRIMARY KEY,
            review_task_id INTEGER NOT NULL REFERENCES review_tasks(id),
            command_type TEXT NOT NULL
                CHECK (command_type IN ('add_label', 'post_comment')),
            payload JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN (
                    'proposed', 'approved', 'rejected',
                    'executing', 'executed', 'failed'
                )),
            idempotency_key TEXT NOT NULL UNIQUE,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            executed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_github_commands_review_task_id
            ON github_commands (review_task_id);
        CREATE INDEX IF NOT EXISTS idx_github_commands_status
            ON github_commands (status);
        """
    )


def downgrade() -> None:
    # The baseline intentionally has no destructive downgrade for existing volumes.
    pass

