"""增加运行追踪、评测报告和事务 Outbox。"""

from alembic import op

revision = "0002_trace_eval_outbox"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS trace_id UUID;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS model_name TEXT;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS prompt_version TEXT;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS agent_version TEXT;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS agent_mode TEXT;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS duration_ms BIGINT;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS input_tokens INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS output_tokens INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS structured_output_success BOOLEAN;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS error_type TEXT;
        ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS estimated_cost_usd NUMERIC(14, 8);

        UPDATE agent_runs SET trace_id = gen_random_uuid() WHERE trace_id IS NULL;
        ALTER TABLE agent_runs ALTER COLUMN trace_id SET DEFAULT gen_random_uuid();
        ALTER TABLE agent_runs ALTER COLUMN trace_id SET NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_trace_id ON agent_runs (trace_id);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at ON agent_runs (created_at DESC);

        ALTER TABLE github_commands ADD COLUMN IF NOT EXISTS error_type TEXT;
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS retry_safe BOOLEAN NOT NULL DEFAULT FALSE;

        CREATE TABLE IF NOT EXISTS agent_node_traces (
            id BIGSERIAL PRIMARY KEY,
            trace_id UUID NOT NULL,
            agent_run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            node_name TEXT NOT NULL,
            sequence_number INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            duration_ms BIGINT,
            input_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            output_summary JSONB,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            error_type TEXT,
            error_message TEXT,
            UNIQUE (agent_run_id, sequence_number)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_node_traces_trace_id
            ON agent_node_traces (trace_id, sequence_number);

        CREATE TABLE IF NOT EXISTS outbox_events (
            id BIGSERIAL PRIMARY KEY,
            event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL CHECK (event_type IN ('agent_run', 'review_commands')),
            aggregate_id INTEGER NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'dispatched', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            locked_at TIMESTAMPTZ,
            rq_job_id TEXT,
            error_type TEXT,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            dispatched_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_dispatch
            ON outbox_events (status, available_at, id);

        CREATE TABLE IF NOT EXISTS eval_reports (
            id BIGSERIAL PRIMARY KEY,
            eval_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
            dataset_name TEXT NOT NULL,
            dataset_hash TEXT NOT NULL,
            runner_type TEXT NOT NULL,
            model_name TEXT,
            prompt_version TEXT NOT NULL,
            agent_version TEXT NOT NULL,
            agent_mode TEXT NOT NULL,
            case_count INTEGER NOT NULL,
            metrics JSONB NOT NULL,
            report_json JSONB NOT NULL,
            publishable_model_score BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_eval_reports_created_at
            ON eval_reports (created_at DESC);

        INSERT INTO outbox_events (
            event_key, event_type, aggregate_id, payload, status, max_attempts
        )
        SELECT 'agent-run:' || ar.id, 'agent_run', ar.id,
               jsonb_build_object('agent_run_id', ar.id), 'pending', 5
        FROM agent_runs ar
        WHERE ar.status = 'pending' AND ar.rq_job_id IS NULL
        ON CONFLICT (event_key) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Trace and recovery data are intentionally retained; no destructive downgrade.
    pass
