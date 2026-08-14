-- Recover group resources orphaned when a Telegram promoter account is banned.

CREATE TABLE IF NOT EXISTS acquisition_group_failover_task (
    id SERIAL PRIMARY KEY,
    source_membership_id INTEGER NOT NULL
        REFERENCES group_account_membership(id) ON DELETE CASCADE,
    source_account_id INTEGER NOT NULL
        REFERENCES telegram_account(id) ON DELETE CASCADE,
    target_account_id INTEGER
        REFERENCES telegram_account(id) ON DELETE SET NULL,
    group_id INTEGER NOT NULL
        REFERENCES "group"(id) ON DELETE CASCADE,
    telegram_group_id BIGINT NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    reason VARCHAR(255),
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMP,
    last_attempt_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_group_failover_source_membership UNIQUE (source_membership_id)
);

CREATE INDEX IF NOT EXISTS idx_group_failover_status_retry
    ON acquisition_group_failover_task(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_group_failover_source_account
    ON acquisition_group_failover_task(source_account_id, status);
CREATE INDEX IF NOT EXISTS idx_group_failover_target_account
    ON acquisition_group_failover_task(target_account_id, status);
CREATE INDEX IF NOT EXISTS idx_group_failover_group
    ON acquisition_group_failover_task(group_id);
