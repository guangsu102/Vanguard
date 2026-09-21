-- Promoter account asset tier metadata and indexes.

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS asset_tier VARCHAR(30) NOT NULL DEFAULT 'unknown';

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS registered_at TIMESTAMP;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS asset_verified_at TIMESTAMP;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS asset_note VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_account_asset_tier
    ON telegram_account(asset_tier);

CREATE INDEX IF NOT EXISTS idx_account_registered_at
    ON telegram_account(registered_at);

COMMENT ON COLUMN telegram_account.asset_tier
    IS 'Promoter account asset tier: unknown/month_1/month_3_6/year_1/year_2/year_3_plus';

COMMENT ON COLUMN telegram_account.registered_at
    IS 'Operator-supplied Telegram account registration time';

COMMENT ON COLUMN telegram_account.asset_verified_at
    IS 'Last time the account asset tier was verified';

COMMENT ON COLUMN telegram_account.asset_note
    IS 'Operator note for account source or purchase batch';
