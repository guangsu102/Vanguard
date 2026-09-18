-- Official @SpamBot account diagnostics and durable restriction provenance.
--
-- AccountStatus is a native PostgreSQL enum whose stored labels are enum member
-- names. The curated runner commits this whole file before 054 uses the new
-- value, so PostgreSQL never uses the label in its ADD VALUE transaction.
ALTER TYPE accountstatus ADD VALUE IF NOT EXISTS 'RESTRICTED';

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS spam_check_status VARCHAR(20) NOT NULL DEFAULT 'unknown';
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS spam_checked_at TIMESTAMP WITHOUT TIME ZONE NULL;
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS spam_check_summary VARCHAR(255) NULL;
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS spam_restriction_confirmed_at TIMESTAMP WITHOUT TIME ZONE NULL;
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS restriction_previous_status VARCHAR(20) NULL;
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS restriction_source VARCHAR(32) NULL;
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS restriction_reason VARCHAR(255) NULL;
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS restriction_detected_at TIMESTAMP WITHOUT TIME ZONE NULL;

CREATE INDEX IF NOT EXISTS idx_account_spam_check_status
    ON telegram_account (spam_check_status);

CREATE TABLE IF NOT EXISTS account_spam_check_operation (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    account_ids_snapshot TEXT NOT NULL,
    snapshot_hash VARCHAR(64) NOT NULL,
    trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
    status VARCHAR(24) NOT NULL DEFAULT 'queued',
    total_accounts INTEGER NOT NULL,
    processed_accounts INTEGER NOT NULL DEFAULT 0,
    clear_accounts INTEGER NOT NULL DEFAULT 0,
    restricted_accounts INTEGER NOT NULL DEFAULT 0,
    failed_accounts INTEGER NOT NULL DEFAULT 0,
    cancelled_accounts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error VARCHAR(255),
    created_by_id INTEGER,
    cancel_requested_at TIMESTAMP WITHOUT TIME ZONE,
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT account_spam_total_accounts_range
        CHECK (total_accounts >= 1 AND total_accounts <= 2000),
    CONSTRAINT account_spam_max_attempts_positive CHECK (max_attempts >= 1)
);

CREATE INDEX IF NOT EXISTS idx_account_spam_operation_status_created
    ON account_spam_check_operation (status, created_at);

CREATE TABLE IF NOT EXISTS account_spam_check_item (
    id SERIAL PRIMARY KEY,
    operation_id INTEGER NOT NULL
        REFERENCES account_spam_check_operation(id) ON DELETE CASCADE,
    account_id INTEGER
        REFERENCES telegram_account(id) ON DELETE SET NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    result VARCHAR(20),
    attempts INTEGER NOT NULL DEFAULT 0,
    reason_code VARCHAR(80),
    response_summary VARCHAR(255),
    account_status_before VARCHAR(20),
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    lease_id VARCHAR(64),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    checked_at TIMESTAMP WITHOUT TIME ZONE,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_account_spam_item_operation_account
        UNIQUE (operation_id, account_id),
    CONSTRAINT account_spam_attempts_non_negative CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_account_spam_item_operation_status
    ON account_spam_check_item (operation_id, status);
CREATE INDEX IF NOT EXISTS idx_account_spam_item_status_retry
    ON account_spam_check_item (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_account_spam_item_account_created
    ON account_spam_check_item (account_id, created_at);
