ALTER TABLE telegram_account_operation_config
    ADD COLUMN IF NOT EXISTS operation_mode VARCHAR(20) NOT NULL DEFAULT 'growth';

CREATE INDEX IF NOT EXISTS idx_account_operation_mode
    ON telegram_account_operation_config (operation_mode);
