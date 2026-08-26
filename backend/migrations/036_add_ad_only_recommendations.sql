CREATE TABLE IF NOT EXISTS group_ad_only_assessment (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
    telegram_group_id BIGINT NOT NULL,
    source_growth_account_id INTEGER
        REFERENCES telegram_account(id) ON DELETE SET NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'observing',
    rule_version VARCHAR(32) NOT NULL DEFAULT 'ad-only-v1',
    completed_sample_count INTEGER NOT NULL DEFAULT 0,
    consecutive_success_count INTEGER NOT NULL DEFAULT 0,
    send_success_percent INTEGER NOT NULL DEFAULT 0,
    survival_24h_percent INTEGER NOT NULL DEFAULT 0,
    pending_sample_count INTEGER NOT NULL DEFAULT 0,
    group_failure_count INTEGER NOT NULL DEFAULT 0,
    deleted_sample_count INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    blocking_reasons_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    evidence_hash VARCHAR(64) NOT NULL,
    sample_window_started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    sample_window_ended_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    valid_until TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_group_ad_only_assessment_group
    ON group_ad_only_assessment (group_id, created_at);

CREATE INDEX IF NOT EXISTS idx_group_ad_only_assessment_status
    ON group_ad_only_assessment (status, valid_until);

CREATE TABLE IF NOT EXISTS group_ad_handover (
    id SERIAL PRIMARY KEY,
    assessment_id INTEGER NOT NULL
        REFERENCES group_ad_only_assessment(id) ON DELETE RESTRICT,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
    active_group_key INTEGER,
    source_growth_account_id INTEGER NOT NULL
        REFERENCES telegram_account(id) ON DELETE RESTRICT,
    target_ad_only_account_id INTEGER NOT NULL
        REFERENCES telegram_account(id) ON DELETE RESTRICT,
    creative_id INTEGER NOT NULL
        REFERENCES ad_creative(id) ON DELETE RESTRICT,
    campaign_id INTEGER REFERENCES ad_campaign(id) ON DELETE SET NULL,
    invite_link_encrypted TEXT,
    invite_secret_expires_at TIMESTAMP WITHOUT TIME ZONE,
    send_mode VARCHAR(30) NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 180,
    scheduled_times TEXT,
    estimated_daily_sends INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    current_step VARCHAR(50) NOT NULL DEFAULT 'queued',
    idempotency_key VARCHAR(64) NOT NULL,
    requested_by_user_id INTEGER NOT NULL,
    approved_by_user_id INTEGER NOT NULL,
    approved_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    completed_at TIMESTAMP WITHOUT TIME ZONE,
    failed_at TIMESTAMP WITHOUT TIME ZONE,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_group_ad_handover_active_group
        UNIQUE (active_group_key),
    CONSTRAINT uq_group_ad_handover_idempotency
        UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_group_ad_handover_status
    ON group_ad_handover (status, updated_at);

CREATE INDEX IF NOT EXISTS idx_group_ad_handover_group
    ON group_ad_handover (group_id, created_at);

CREATE TABLE IF NOT EXISTS group_ad_only_event (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
    assessment_id INTEGER
        REFERENCES group_ad_only_assessment(id) ON DELETE CASCADE,
    handover_id INTEGER REFERENCES group_ad_handover(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    step VARCHAR(50),
    status VARCHAR(30),
    actor_user_id INTEGER,
    message VARCHAR(500),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_group_ad_only_event_group
    ON group_ad_only_event (group_id, created_at);

CREATE INDEX IF NOT EXISTS idx_group_ad_only_event_assessment
    ON group_ad_only_event (assessment_id, created_at);

CREATE INDEX IF NOT EXISTS idx_group_ad_only_event_handover
    ON group_ad_only_event (handover_id, created_at);
