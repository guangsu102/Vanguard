-- Account operation business stage for dynamic promotion frequency.

ALTER TABLE telegram_account_operation_config
    ADD COLUMN IF NOT EXISTS business_stage VARCHAR(20);

UPDATE telegram_account_operation_config
    SET business_stage = 'new'
    WHERE business_stage IS NULL;

ALTER TABLE telegram_account_operation_config
    ALTER COLUMN business_stage SET DEFAULT 'new';

ALTER TABLE telegram_account_operation_config
    ALTER COLUMN business_stage SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_account_operation_business_stage
    ON telegram_account_operation_config(business_stage);

COMMENT ON COLUMN telegram_account_operation_config.business_stage
    IS 'Automation business stage: new/normal/hot/cooldown';
