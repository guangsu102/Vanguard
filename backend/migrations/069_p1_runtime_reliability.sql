-- P1 runtime reliability: additive columns and indexes only.
-- Historical inbox/audit records are intentionally left untouched.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- Acquire both DDL locks before altering either table. A busy application
-- transaction makes this fail immediately instead of holding one table while
-- waiting for the other and blocking the live inbox/qualification pipeline.
LOCK TABLE group_qualification_audit, telegram_event_inbox
    IN ACCESS EXCLUSIVE MODE NOWAIT;

ALTER TABLE telegram_event_inbox
    ADD COLUMN IF NOT EXISTS lane VARCHAR(24) NOT NULL DEFAULT 'business',
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMP NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS claim_owner VARCHAR(120),
    ADD COLUMN IF NOT EXISTS claim_lease_expires_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS coalesce_key VARCHAR(180),
    ADD COLUMN IF NOT EXISTS coalesced_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS checkpoint_stage VARCHAR(64),
    ADD COLUMN IF NOT EXISTS checkpoint_revision VARCHAR(128),
    ADD COLUMN IF NOT EXISTS checkpoint_json TEXT,
    ADD COLUMN IF NOT EXISTS checkpoint_updated_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_lane_ready
    ON telegram_event_inbox (lane, state, available_at, id);
CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_claim_lease
    ON telegram_event_inbox (state, claim_lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_coalesce
    ON telegram_event_inbox (account_id, coalesce_key, state);

ALTER TABLE group_qualification_audit
    ADD COLUMN IF NOT EXISTS checkpoint_stage VARCHAR(64),
    ADD COLUMN IF NOT EXISTS checkpoint_revision VARCHAR(128),
    ADD COLUMN IF NOT EXISTS checkpoint_json TEXT,
    ADD COLUMN IF NOT EXISTS checkpoint_updated_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_group_qualification_checkpoint
    ON group_qualification_audit (membership_id, checkpoint_revision, checkpoint_stage);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE c.conname = 'ck_telegram_event_inbox_lane'
           AND t.relname = 'telegram_event_inbox'
           AND n.nspname = current_schema()
    ) THEN
        ALTER TABLE telegram_event_inbox
            ADD CONSTRAINT ck_telegram_event_inbox_lane
            CHECK (lane IN ('listener_fact', 'business'));
    END IF;
END $$;
