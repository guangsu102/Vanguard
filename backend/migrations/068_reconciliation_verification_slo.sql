-- Additive follow-up to 067; retain the checksum of the deployed migration.
-- No historical business records or task creation timestamps are rewritten.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE reconciliation_task ADD COLUMN IF NOT EXISTS verification_due_at TIMESTAMP;
