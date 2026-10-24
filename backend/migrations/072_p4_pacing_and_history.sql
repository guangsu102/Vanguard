-- P4 tables for the canonical SQL migration runner, after P0-P3.
-- Generated from 051_p4_pacing_and_history; safe to replay.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS growth_pacing_decision (
    id SERIAL NOT NULL,
    stage VARCHAR(24) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    window_started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    window_seconds INTEGER NOT NULL,
    candidate_count INTEGER DEFAULT '0' NOT NULL,
    gated_count INTEGER DEFAULT '0' NOT NULL,
    scheduled_count INTEGER DEFAULT '0' NOT NULL,
    selected_account_id INTEGER,
    applied BOOLEAN DEFAULT false NOT NULL,
    gate_reasons_json TEXT DEFAULT '{}' NOT NULL,
    candidates_json TEXT DEFAULT '[]' NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT pacing_decision_counts CHECK (candidate_count >= 0 AND gated_count >= 0 AND scheduled_count >= 0),
    FOREIGN KEY(selected_account_id) REFERENCES telegram_account (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_growth_pacing_decision_recent ON growth_pacing_decision (created_at);

CREATE INDEX IF NOT EXISTS ix_growth_pacing_decision_stage ON growth_pacing_decision (stage, created_at);

CREATE TABLE IF NOT EXISTS growth_pacing_daily_account_stat (
    id SERIAL NOT NULL,
    stat_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    account_id INTEGER NOT NULL,
    decision_count INTEGER DEFAULT '0' NOT NULL,
    candidate_count INTEGER DEFAULT '0' NOT NULL,
    gated_count INTEGER DEFAULT '0' NOT NULL,
    scheduled_count INTEGER DEFAULT '0' NOT NULL,
    avg_score FLOAT DEFAULT '0' NOT NULL,
    fair_share_avg FLOAT DEFAULT '0' NOT NULL,
    actual_share_avg FLOAT DEFAULT '0' NOT NULL,
    ad_success_count INTEGER DEFAULT '0' NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT pacing_daily_stat_counts CHECK (candidate_count >= 0 AND gated_count >= 0 AND scheduled_count >= 0 AND decision_count >= 0 AND ad_success_count >= 0),
    FOREIGN KEY(account_id) REFERENCES telegram_account (id) ON DELETE CASCADE,
    CONSTRAINT uq_pacing_daily_account_stat UNIQUE (stat_date, account_id)
);

CREATE INDEX IF NOT EXISTS ix_pacing_daily_account_stat_date ON growth_pacing_daily_account_stat (stat_date);

CREATE TABLE IF NOT EXISTS growth_history_governance_batch (
    id SERIAL NOT NULL,
    mode VARCHAR(16) NOT NULL,
    status VARCHAR(16) DEFAULT 'running' NOT NULL,
    dry_run BOOLEAN DEFAULT true NOT NULL,
    cutoffs_json TEXT DEFAULT '{}' NOT NULL,
    candidates_json TEXT DEFAULT '{}' NOT NULL,
    archived_count INTEGER DEFAULT '0' NOT NULL,
    deleted_count INTEGER DEFAULT '0' NOT NULL,
    skipped_json TEXT DEFAULT '{}' NOT NULL,
    checksum VARCHAR(64),
    error TEXT,
    started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT history_batch_mode CHECK (mode IN ('dry_run','archive','cleanup')),
    CONSTRAINT history_batch_status CHECK (status IN ('running','succeeded','failed')),
    CONSTRAINT history_batch_counts CHECK (archived_count >= 0 AND deleted_count >= 0)
);

CREATE INDEX IF NOT EXISTS ix_history_governance_batch_created ON growth_history_governance_batch (created_at);

CREATE INDEX IF NOT EXISTS ix_history_governance_batch_mode ON growth_history_governance_batch (mode, created_at);

CREATE TABLE IF NOT EXISTS growth_operation_archive (
    id SERIAL NOT NULL,
    operation_id BIGINT NOT NULL,
    batch_id INTEGER NOT NULL,
    kind VARCHAR(24) NOT NULL,
    account_id INTEGER NOT NULL,
    group_id INTEGER,
    campaign_id INTEGER,
    target_key VARCHAR(500) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    snapshot_id VARCHAR(128) NOT NULL,
    group_version VARCHAR(128) NOT NULL,
    rule_version VARCHAR(128) NOT NULL,
    evidence_hash VARCHAR(64) NOT NULL,
    budget_reservation_id VARCHAR(160),
    payload_hash VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL,
    state_version INTEGER NOT NULL,
    reconciliation_required BOOLEAN DEFAULT false NOT NULL,
    provider_request_id VARCHAR(160),
    provider_receipt_json TEXT,
    attempt_count INTEGER DEFAULT '0' NOT NULL,
    reserved_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    sent_at TIMESTAMP WITHOUT TIME ZONE,
    confirmed_at TIMESTAMP WITHOUT TIME ZONE,
    survival_due_at TIMESTAMP WITHOUT TIME ZONE,
    resolved_at TIMESTAMP WITHOUT TIME ZONE,
    outbox_json TEXT DEFAULT '{}' NOT NULL,
    attempts_json TEXT DEFAULT '[]' NOT NULL,
    events_json TEXT DEFAULT '[]' NOT NULL,
    archived_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT archive_terminal_state CHECK (state IN ('resolved','failed','cancelled')),
    FOREIGN KEY(batch_id) REFERENCES growth_history_governance_batch (id) ON DELETE RESTRICT,
    CONSTRAINT uq_growth_operation_archive_operation UNIQUE (operation_id)
);

CREATE INDEX IF NOT EXISTS ix_growth_operation_archive_archived_at ON growth_operation_archive (archived_at);

CREATE INDEX IF NOT EXISTS ix_growth_operation_archive_account ON growth_operation_archive (account_id, archived_at);

CREATE INDEX IF NOT EXISTS ix_growth_operation_terminal_resolved ON growth_operation (state, resolved_at) WHERE state IN ('resolved','failed','cancelled') AND reconciliation_required = false;

CREATE INDEX IF NOT EXISTS ix_growth_operation_unresolved ON growth_operation (updated_at) WHERE reconciliation_required = true OR state IN ('reserved','sent','receipt_pending','confirmed','survival_pending');

CREATE INDEX IF NOT EXISTS ix_growth_outbox_terminal_completed ON growth_outbox (state, completed_at) WHERE state IN ('succeeded','dead','cancelled');

CREATE INDEX IF NOT EXISTS ix_growth_outbox_open_available ON growth_outbox (kind, available_at) WHERE state IN ('pending','retryable_failed');
