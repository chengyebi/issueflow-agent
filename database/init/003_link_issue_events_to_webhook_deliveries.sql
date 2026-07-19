ALTER TABLE issue_events
ADD COLUMN IF NOT EXISTS webhook_delivery_id INTEGER
REFERENCES webhook_deliveries(id);

CREATE INDEX IF NOT EXISTS idx_issue_events_webhook_delivery_id
ON issue_events (webhook_delivery_id);
