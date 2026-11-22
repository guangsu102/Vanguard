-- Additive only: no existing audit is rewritten and no rollout is enabled.
SET LOCAL lock_timeout = '10s';
ALTER TABLE acquisition_auto_join_attempt ADD COLUMN IF NOT EXISTS sdk_entered_at TIMESTAMP;
ALTER TABLE acquisition_auto_join_attempt ADD COLUMN IF NOT EXISTS platform_responded_at TIMESTAMP;
CREATE TABLE IF NOT EXISTS qualification_active_grant (
    membership_id INTEGER PRIMARY KEY REFERENCES group_account_membership(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES "group"(id),
    audit_id INTEGER REFERENCES group_qualification_audit(id) ON DELETE SET NULL,
    scope_key VARCHAR(64) NOT NULL,
    membership_joined_at TIMESTAMP,
    policy_version VARCHAR(64) NOT NULL,
    profile_fingerprint VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    invalidated_at TIMESTAMP,
    invalidated_reason VARCHAR(160),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qualification_grant_account
    ON qualification_active_grant(account_id, invalidated_at);
CREATE TABLE IF NOT EXISTS qualification_work (
    id SERIAL PRIMARY KEY,
    membership_id INTEGER NOT NULL REFERENCES group_account_membership(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES "group"(id),
    membership_joined_at TIMESTAMP,
    engine_version VARCHAR(32) NOT NULL DEFAULT 'efficiency_v1',
    family VARCHAR(32) NOT NULL DEFAULT 'qualification',
    scope_key VARCHAR(64) NOT NULL UNIQUE,
    policy_version VARCHAR(64) NOT NULL,
    profile_fingerprint VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL DEFAULT 'initial',
    state VARCHAR(32) NOT NULL DEFAULT 'READY',
    reason VARCHAR(160),
    revision INTEGER NOT NULL DEFAULT 1,
    event_version VARCHAR(64) NOT NULL DEFAULT '',
    event_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT,
    checkpoint_stage VARCHAR(64),
    checkpoint_revision VARCHAR(128),
    claim_token VARCHAR(64),
    claim_until TIMESTAMP,
    ready_at TIMESTAMP,
    deadline_at TIMESTAMP,
    next_step_reads INTEGER NOT NULL DEFAULT 0,
    missing_dependencies_json TEXT NOT NULL DEFAULT '[]',
    initial_collection_pending BOOLEAN NOT NULL DEFAULT FALSE,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qualification_work_due
    ON qualification_work(account_id, state, ready_at);
CREATE INDEX IF NOT EXISTS idx_qualification_work_member
    ON qualification_work(membership_id, scope_key);
