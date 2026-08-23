ALTER TABLE group_ad_profile
    ADD COLUMN IF NOT EXISTS ad_policy_evidence_hash VARCHAR(64) NULL;

COMMENT ON COLUMN group_ad_profile.ad_policy_evidence_hash
    IS 'Stable hash of the latest advertisement policy audit evidence';
