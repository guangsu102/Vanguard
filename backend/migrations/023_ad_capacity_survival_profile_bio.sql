-- Advertisement capacity/survival tracking and Telegram public bio fields.

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS profile_bio VARCHAR(255);

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS profile_bio_synced_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS group_ad_profile (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
    telegram_group_id BIGINT NOT NULL,
    ad_tier VARCHAR(30) NOT NULL DEFAULT 'low',
    daily_capacity INTEGER NOT NULL DEFAULT 20,
    score INTEGER NOT NULL DEFAULT 0,
    survival_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    consecutive_survivals INTEGER NOT NULL DEFAULT 0,
    consecutive_deletions INTEGER NOT NULL DEFAULT 0,
    last_probe_at TIMESTAMP,
    last_survived_at TIMESTAMP,
    last_deleted_at TIMESTAMP,
    blocked_at TIMESTAMP,
    blocked_reason VARCHAR(255),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_group_ad_profile_group UNIQUE (group_id)
);

CREATE INDEX IF NOT EXISTS idx_group_ad_profile_tg_group
    ON group_ad_profile(telegram_group_id);

CREATE INDEX IF NOT EXISTS idx_group_ad_profile_tier
    ON group_ad_profile(ad_tier);

CREATE INDEX IF NOT EXISTS idx_group_ad_profile_blocked
    ON group_ad_profile(blocked_at);

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS ad_status VARCHAR(40) NOT NULL DEFAULT 'warming';

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS account_group_daily_cap INTEGER NOT NULL DEFAULT 400;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS interaction_started_at TIMESTAMP;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS interaction_sent_today INTEGER NOT NULL DEFAULT 0;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS first_ad_allowed_at TIMESTAMP;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS last_ad_survived_at TIMESTAMP;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS last_ad_deleted_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_group_membership_ad_status
    ON group_account_membership(account_id, ad_status);

ALTER TABLE ad_delivery_log
    ADD COLUMN IF NOT EXISTS survival_status VARCHAR(30) NOT NULL DEFAULT 'not_required';

ALTER TABLE ad_delivery_log
    ADD COLUMN IF NOT EXISTS survival_check_due_at TIMESTAMP;

ALTER TABLE ad_delivery_log
    ADD COLUMN IF NOT EXISTS survival_checked_at TIMESTAMP;

ALTER TABLE ad_delivery_log
    ADD COLUMN IF NOT EXISTS survival_error TEXT;

CREATE INDEX IF NOT EXISTS idx_ad_delivery_survival_due
    ON ad_delivery_log(survival_status, survival_check_due_at);

COMMENT ON COLUMN telegram_account.profile_bio
    IS 'Telegram public profile bio configured by operators';

COMMENT ON COLUMN telegram_account.profile_bio_synced_at
    IS 'Last time profile_bio was synced to Telegram';

COMMENT ON TABLE group_ad_profile
    IS 'Per-group soft advertisement capacity and survival profile';

COMMENT ON COLUMN group_account_membership.ad_status
    IS 'Account-group soft-ad status: warming/probing/active/paused/blocked';

COMMENT ON COLUMN group_account_membership.account_group_daily_cap
    IS 'Single-account single-group daily soft-ad cap';

COMMENT ON COLUMN ad_delivery_log.survival_status
    IS 'Post-delivery soft-ad survival check status';
