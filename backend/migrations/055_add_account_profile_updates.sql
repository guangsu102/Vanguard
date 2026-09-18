-- Durable serial batch profile updates for promoter accounts in ad_only mode.
CREATE TABLE IF NOT EXISTS account_profile_update_operation (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    account_ids_snapshot TEXT NOT NULL,
    snapshot_hash VARCHAR(64) NOT NULL,
    profile_bio VARCHAR(70) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'queued',
    total_accounts INTEGER NOT NULL,
    processed_accounts INTEGER NOT NULL DEFAULT 0,
    succeeded_accounts INTEGER NOT NULL DEFAULT 0,
    failed_accounts INTEGER NOT NULL DEFAULT 0,
    cancelled_accounts INTEGER NOT NULL DEFAULT 0,
    skipped_accounts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error VARCHAR(255),
    created_by_id INTEGER,
    cancel_requested_at TIMESTAMP WITHOUT TIME ZONE,
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT account_profile_update_total_accounts_range
        CHECK (total_accounts >= 1 AND total_accounts <= 2000),
    CONSTRAINT account_profile_update_max_attempts_positive CHECK (max_attempts >= 1)
);

CREATE TABLE IF NOT EXISTS account_profile_update_item (
    id SERIAL PRIMARY KEY,
    operation_id INTEGER NOT NULL
        REFERENCES account_profile_update_operation(id) ON DELETE CASCADE,
    account_id INTEGER
        REFERENCES telegram_account(id) ON DELETE SET NULL,
    baseline_bio_hash VARCHAR(64) NOT NULL,
    desired_bio_hash VARCHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason_code VARCHAR(80),
    error_message VARCHAR(255),
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    lease_id VARCHAR(64),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    remote_attempted_at TIMESTAMP WITHOUT TIME ZONE,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_account_profile_update_item_operation_account
        UNIQUE (operation_id, account_id),
    CONSTRAINT account_profile_update_attempts_non_negative CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_account_profile_update_operation_status_created
    ON account_profile_update_operation (status, created_at);
CREATE INDEX IF NOT EXISTS idx_account_profile_update_item_operation_status
    ON account_profile_update_item (operation_id, status);
CREATE INDEX IF NOT EXISTS idx_account_profile_update_item_status_retry
    ON account_profile_update_item (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_account_profile_update_item_account_created
    ON account_profile_update_item (account_id, created_at);

-- A single durable lease makes profile mutations serial even if a second worker
-- process is accidentally started during a deployment or recovery.
CREATE TABLE IF NOT EXISTS account_profile_update_queue_lease (
    id INTEGER PRIMARY KEY,
    lease_id VARCHAR(64),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT account_profile_update_queue_lease_singleton CHECK (id = 1)
);
INSERT INTO account_profile_update_queue_lease (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;
