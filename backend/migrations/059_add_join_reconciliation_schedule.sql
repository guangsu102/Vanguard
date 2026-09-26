-- Add durable fair scheduling. Existing request timestamps and quotas stay intact.
ALTER TABLE acquisition_auto_join_attempt
    ADD COLUMN IF NOT EXISTS reconciliation_checked_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS reconciliation_next_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_auto_join_reconciliation_due
    ON acquisition_auto_join_attempt (reconciliation_next_at, request_sent_at);
