-- Durable state for the official managed Bot provisioning workflow.
CREATE TABLE IF NOT EXISTS managed_bot_provision (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    request_hash VARCHAR(64) NOT NULL,
    owner_account_id INTEGER NOT NULL
        REFERENCES telegram_account(id) ON DELETE RESTRICT,
    manager_bot_profile_id INTEGER NOT NULL
        REFERENCES guardian_bot_profile(id) ON DELETE RESTRICT,
    display_name VARCHAR(64) NOT NULL,
    username VARCHAR(32) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'queued',
    current_step VARCHAR(32) NOT NULL DEFAULT 'preflight',
    bot_user_id BIGINT UNIQUE,
    guardian_bot_profile_id INTEGER
        REFERENCES guardian_bot_profile(id) ON DELETE SET NULL,
    owned_bot_profile_id INTEGER
        REFERENCES owned_bot_profiles(id) ON DELETE SET NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    error_code VARCHAR(80),
    error_message VARCHAR(255),
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    celery_task_id VARCHAR(64),
    lease_id VARCHAR(64),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
    create_attempted_at TIMESTAMP WITHOUT TIME ZONE,
    external_created_at TIMESTAMP WITHOUT TIME ZONE,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_by_id INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT managed_bot_provision_attempts_non_negative CHECK (attempts >= 0),
    CONSTRAINT managed_bot_provision_max_attempts_positive CHECK (max_attempts >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_bot_provision_reserved_username
    ON managed_bot_provision (username)
    WHERE status IN ('queued', 'running', 'retry_wait', 'needs_attention', 'succeeded')
       OR bot_user_id IS NOT NULL
       OR external_created_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_managed_bot_provision_status_retry
    ON managed_bot_provision (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_managed_bot_provision_owner
    ON managed_bot_provision (owner_account_id);
CREATE INDEX IF NOT EXISTS idx_managed_bot_provision_manager
    ON managed_bot_provision (manager_bot_profile_id);
