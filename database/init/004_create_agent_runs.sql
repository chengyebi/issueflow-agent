CREATE TABLE IF NOT EXISTS agent_runs (
    id SERIAL PRIMARY KEY,
    issue_event_id INTEGER NOT NULL UNIQUE
        REFERENCES issue_events(id),
    status TEXT NOT NULL DEFAULT 'pending',
    rq_job_id TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_message TEXT,
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status
ON agent_runs (status);
