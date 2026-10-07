ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_due_at TIMESTAMP;
ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_log_id INTEGER;
ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_token VARCHAR(64);
ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_expires_at TIMESTAMP;
ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_retry_at TIMESTAMP;
ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_error VARCHAR(160);
ALTER TABLE group_ad_frequency ADD COLUMN IF NOT EXISTS daily_review_checked_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_group_ad_frequency_daily_review_due ON group_ad_frequency (daily_review_due_at) WHERE status = 'active';
