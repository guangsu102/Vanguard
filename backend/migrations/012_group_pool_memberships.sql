-- Add group-pool source fields and account-to-group membership tracking.
ALTER TABLE "group"
    ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS discovery_source VARCHAR(50) NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS source_keyword VARCHAR(255),
    ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMP NULL;

CREATE INDEX IF NOT EXISTS idx_group_status ON "group" (status);
CREATE INDEX IF NOT EXISTS idx_group_source_keyword ON "group" (source_keyword);

CREATE TABLE IF NOT EXISTS group_account_membership (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
    telegram_group_id BIGINT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    status VARCHAR(30) NOT NULL DEFAULT 'joined',
    join_method VARCHAR(50) NOT NULL DEFAULT 'manual',
    source_keyword VARCHAR(255),
    joined_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    left_at TIMESTAMP NULL,
    last_checked_at TIMESTAMP NULL,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_group_account_membership_group_account UNIQUE (group_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_group_membership_group ON group_account_membership (group_id);
CREATE INDEX IF NOT EXISTS idx_group_membership_account ON group_account_membership (account_id);
CREATE INDEX IF NOT EXISTS idx_group_membership_tg_group ON group_account_membership (telegram_group_id);
CREATE INDEX IF NOT EXISTS idx_group_membership_status ON group_account_membership (status);
