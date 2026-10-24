-- P2 durable join/ad operation state and account-Actor outbox.
-- Additive only. Existing AutoJoinAttempt and AdDeliveryLog rows remain the
-- legacy read model until their callers opt into this contract.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS growth_operation (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(24) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE RESTRICT,
    group_id INTEGER REFERENCES "group"(id) ON DELETE SET NULL,
    campaign_id INTEGER REFERENCES ad_campaign(id) ON DELETE SET NULL,
    target_key VARCHAR(500) NOT NULL,
    run_id VARCHAR(128),
    -- The immutable qualification snapshot table is introduced by the
    -- following P3 migration.  Keep this column structurally required in P2
    -- while adding the foreign key after that table exists in 071.
    snapshot_id VARCHAR(128) NOT NULL,
    group_version VARCHAR(128) NOT NULL,
    rule_version VARCHAR(128) NOT NULL,
    evidence_hash VARCHAR(64) NOT NULL,
    budget_reservation_id VARCHAR(160)
        REFERENCES account_budget_reservation(reservation_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_hash VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'reserved',
    state_version INTEGER NOT NULL DEFAULT 0,
    reconciliation_required BOOLEAN NOT NULL DEFAULT FALSE,
    provider_request_id VARCHAR(160),
    provider_receipt_json TEXT,
    last_error VARCHAR(500),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    reserved_at TIMESTAMP NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMP,
    confirmed_at TIMESTAMP,
    survival_due_at TIMESTAMP,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_growth_operation_kind
        CHECK (kind IN ('join_group','send_ad')),
    CONSTRAINT ck_growth_operation_state
        CHECK (state IN ('reserved','sent','receipt_pending','confirmed','survival_pending','resolved','failed','cancelled')),
    CONSTRAINT ck_growth_operation_versions
        CHECK (state_version >= 0 AND attempt_count >= 0),
    CONSTRAINT ck_growth_operation_reserved_budget
        CHECK (state <> 'reserved' OR budget_reservation_id IS NOT NULL),
    CONSTRAINT ck_growth_operation_snapshot
        CHECK (snapshot_id <> '' AND group_version <> '' AND rule_version <> '' AND char_length(evidence_hash) = 64),
    CONSTRAINT ck_growth_operation_terminal_reconciliation
        CHECK (NOT (state IN ('resolved','failed','cancelled') AND reconciliation_required)),
    CONSTRAINT ck_growth_operation_external_evidence
        CHECK (state NOT IN ('confirmed','survival_pending','resolved') OR provider_receipt_json IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_growth_operation_ready
    ON growth_operation (kind, state, reconciliation_required, updated_at);
CREATE INDEX IF NOT EXISTS ix_growth_operation_target
    ON growth_operation (kind, account_id, target_key, state);
CREATE INDEX IF NOT EXISTS ix_growth_operation_snapshot
    ON growth_operation (snapshot_id, group_version, rule_version);
CREATE UNIQUE INDEX IF NOT EXISTS uq_growth_operation_active_target
    ON growth_operation (account_id, target_key)
    WHERE state IN ('reserved','sent','receipt_pending','confirmed','survival_pending')
       OR reconciliation_required;

-- Snapshot identity is append-only.  State/lease updates may still change
-- operational columns, while a direct SQL UPDATE cannot rewrite the evidence
-- that authorized the action.
CREATE OR REPLACE FUNCTION vanguard_guard_growth_operation_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(NEW.kind, NEW.idempotency_key, NEW.account_id, NEW.group_id,
           NEW.campaign_id, NEW.target_key, NEW.run_id, NEW.snapshot_id,
           NEW.group_version, NEW.rule_version, NEW.evidence_hash,
           NEW.budget_reservation_id, NEW.payload_json, NEW.payload_hash)
       IS DISTINCT FROM
       ROW(OLD.kind, OLD.idempotency_key, OLD.account_id, OLD.group_id,
           OLD.campaign_id, OLD.target_key, OLD.run_id, OLD.snapshot_id,
           OLD.group_version, OLD.rule_version, OLD.evidence_hash,
           OLD.budget_reservation_id, OLD.payload_json, OLD.payload_hash)
    THEN
        RAISE EXCEPTION 'growth operation identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_growth_operation_identity ON growth_operation;
CREATE TRIGGER trg_growth_operation_identity
BEFORE UPDATE ON growth_operation
FOR EACH ROW EXECUTE FUNCTION vanguard_guard_growth_operation_identity();

CREATE TABLE IF NOT EXISTS growth_outbox (
    id SERIAL PRIMARY KEY,
    operation_id INTEGER NOT NULL UNIQUE REFERENCES growth_operation(id) ON DELETE CASCADE,
    kind VARCHAR(24) NOT NULL,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE RESTRICT,
    priority INTEGER NOT NULL DEFAULT 50,
    payload_version INTEGER NOT NULL DEFAULT 1,
    state VARCHAR(32) NOT NULL DEFAULT 'pending',
    available_at TIMESTAMP NOT NULL DEFAULT NOW(),
    lease_owner VARCHAR(160),
    lease_expires_at TIMESTAMP,
    fencing_token VARCHAR(160),
    attempts INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    provider_request_id VARCHAR(160),
    last_error VARCHAR(500),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMP,
    CONSTRAINT ck_growth_outbox_kind
        CHECK (kind IN ('join_group','send_ad')),
    CONSTRAINT ck_growth_outbox_state
        CHECK (state IN ('pending','leased','retryable_failed','succeeded','reconciliation_required','dead','cancelled')),
    CONSTRAINT ck_growth_outbox_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_growth_outbox_payload_version CHECK (payload_version >= 1),
    CONSTRAINT ck_growth_outbox_lease_fields
        CHECK (state <> 'leased' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CONSTRAINT ck_growth_outbox_lease_only_processing
        CHECK (state = 'leased' OR (lease_owner IS NULL AND lease_expires_at IS NULL AND fencing_token IS NULL))
);

CREATE INDEX IF NOT EXISTS ix_growth_outbox_ready
    ON growth_outbox (state, available_at, priority, id);
CREATE INDEX IF NOT EXISTS ix_growth_outbox_lease
    ON growth_outbox (state, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_growth_outbox_account
    ON growth_outbox (account_id, state, available_at);

CREATE TABLE IF NOT EXISTS growth_operation_attempt (
    id SERIAL PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES growth_operation(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    actor_generation VARCHAR(160),
    provider_request_id VARCHAR(160),
    outcome VARCHAR(32) NOT NULL DEFAULT 'started',
    receipt_json TEXT,
    error VARCHAR(500),
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP,
    CONSTRAINT uq_growth_operation_attempt_no UNIQUE (operation_id, attempt_no),
    CONSTRAINT ck_growth_operation_attempt_positive CHECK (attempt_no > 0)
);
CREATE INDEX IF NOT EXISTS ix_growth_operation_attempt_operation
    ON growth_operation_attempt (operation_id, attempt_no);

-- Keep invariants true even for administrative SQL writers.  The application
-- state-transition helper performs the same checks and records the audit
-- event; this trigger is the database backstop for direct writes and races.
CREATE OR REPLACE FUNCTION vanguard_guard_growth_operation_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state_version <= OLD.state_version THEN
        RAISE EXCEPTION 'growth operation state_version must increase';
    END IF;
    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'growth operation attempt_count cannot decrease';
    END IF;
    IF NEW.state IN ('sent','receipt_pending','confirmed','survival_pending','resolved')
       AND NOT EXISTS (
           SELECT 1 FROM growth_operation_attempt a
           WHERE a.operation_id = NEW.id
       ) THEN
        RAISE EXCEPTION 'growth operation sent state requires an attempt';
    END IF;
    IF NEW.state IN ('confirmed','survival_pending','resolved')
       AND NEW.provider_receipt_json IS NULL THEN
        RAISE EXCEPTION 'growth operation confirmation requires provider evidence';
    END IF;
    IF NEW.state = 'reserved' AND NEW.budget_reservation_id IS NULL THEN
        RAISE EXCEPTION 'reserved growth operation requires budget reservation';
    END IF;
    IF NEW.state IN ('resolved','failed','cancelled') AND NEW.reconciliation_required THEN
        RAISE EXCEPTION 'terminal growth operation cannot require reconciliation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_growth_operation_state ON growth_operation;
CREATE TRIGGER trg_growth_operation_state
BEFORE UPDATE ON growth_operation
FOR EACH ROW EXECUTE FUNCTION vanguard_guard_growth_operation_state();

CREATE TABLE IF NOT EXISTS growth_operation_event (
    id SERIAL PRIMARY KEY,
    operation_id INTEGER NOT NULL REFERENCES growth_operation(id) ON DELETE CASCADE,
    event_type VARCHAR(48) NOT NULL,
    from_state VARCHAR(32),
    to_state VARCHAR(32),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_growth_operation_event_operation
    ON growth_operation_event (operation_id, created_at);

ALTER TABLE reconciliation_task
    DROP CONSTRAINT IF EXISTS ck_reconciliation_task_reconciliation_kind;
ALTER TABLE reconciliation_task
    ADD CONSTRAINT ck_reconciliation_task_reconciliation_kind
    CHECK (kind IN ('join','ad','inbox','growth'));

ALTER TABLE reconciliation_task
    ADD COLUMN IF NOT EXISTS reason VARCHAR(500),
    ADD COLUMN IF NOT EXISTS first_discovered_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS age_threshold_seconds INTEGER,
    ADD COLUMN IF NOT EXISTS evidence_json TEXT,
    ADD COLUMN IF NOT EXISTS result_json TEXT;

CREATE INDEX IF NOT EXISTS ix_reconciliation_growth_source
    ON reconciliation_task (kind, source_id, state)
    WHERE kind = 'growth';
