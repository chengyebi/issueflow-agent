CREATE TABLE IF NOT EXISTS github_commands (
    id SERIAL PRIMARY KEY,

    review_task_id INTEGER NOT NULL
        REFERENCES review_tasks(id),

    command_type TEXT NOT NULL
        CHECK (command_type IN (
            'add_label',
            'post_comment'
        )),

    payload JSONB NOT NULL,

    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN (
            'proposed',
            'approved',
            'rejected',
            'executing',
            'executed',
            'failed'
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
