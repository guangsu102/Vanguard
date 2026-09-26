ALTER TABLE telegram_account_operation_config ADD COLUMN IF NOT EXISTS adaptive_ads_enabled BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS group_ad_frequency (
 telegram_group_id BIGINT PRIMARY KEY,
 quota INTEGER NOT NULL DEFAULT 1 CHECK (quota BETWEEN 1 AND 30),
 epoch INTEGER NOT NULL DEFAULT 1,
 epoch_started_at TIMESTAMP NOT NULL DEFAULT now(),
 mature BOOLEAN NOT NULL DEFAULT false,
 status VARCHAR(24) NOT NULL DEFAULT 'active',
 pause_until TIMESTAMP, promote_after TIMESTAMP,
 reason VARCHAR(160), updated_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS group_ad_frequency_event (
 id SERIAL PRIMARY KEY,
 telegram_group_id BIGINT NOT NULL,
 log_id INTEGER NOT NULL REFERENCES ad_delivery_log(id),
 kind VARCHAR(24) NOT NULL, epoch INTEGER NOT NULL,
 old_quota INTEGER NOT NULL, new_quota INTEGER NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT now(),
 CONSTRAINT uq_frequency_log_event UNIQUE(log_id, kind)
);
CREATE INDEX IF NOT EXISTS ix_group_ad_frequency_event_telegram_group_id ON group_ad_frequency_event(telegram_group_id);
