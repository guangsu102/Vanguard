-- Platform-aware api_id/app_hash pooling: declare which device platform each
-- Telegram API config was registered for, so accounts can be matched to a
-- config by device fingerprint platform.
ALTER TABLE telegram_api_config
    ADD COLUMN IF NOT EXISTS platform VARCHAR(16) NOT NULL DEFAULT 'any';
