-- Add keyword auto-replenishment policy fields for account automation config.

ALTER TABLE telegram_account_operation_config
    ADD COLUMN IF NOT EXISTS keyword_auto_replenish_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE telegram_account_operation_config
    ADD COLUMN IF NOT EXISTS keyword_replenish_requires_review BOOLEAN NOT NULL DEFAULT TRUE;
