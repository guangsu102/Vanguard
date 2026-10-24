-- Keep a terminal P2 operation as a permanent idempotency tombstone after
-- history children are compacted. This migration is additive and replayable.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE growth_operation ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_growth_operation_unarchived_terminal
    ON growth_operation (state, resolved_at, updated_at)
    WHERE archived_at IS NULL
      AND state IN ('resolved','failed','cancelled')
      AND reconciliation_required = false;
