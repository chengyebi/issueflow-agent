CREATE TABLE IF NOT EXISTS review_tasks (
    id SERIAL PRIMARY KEY,

    agent_run_id INTEGER NOT NULL UNIQUE
        REFERENCES agent_runs(id),

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'approved',
            'rejected'
        )),

    reviewer TEXT,
    review_note TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_review_tasks_status
ON review_tasks (status);
