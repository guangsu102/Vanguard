ALTER TABLE group_ad_profile
    ADD COLUMN IF NOT EXISTS ad_policy_probe_status VARCHAR(30) NOT NULL DEFAULT 'not_started',
    ADD COLUMN IF NOT EXISTS ad_policy_probe_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS ad_policy_probe_account_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS ad_policy_probe_error TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_group_ad_profile_probe
    ON group_ad_profile (ad_policy_probe_status, ad_policy_probe_at);
