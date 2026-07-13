CREATE TABLE IF NOT EXISTS issue_events (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    repo TEXT NOT NULL,
    action TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    issue_body TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
