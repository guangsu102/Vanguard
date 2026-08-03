-- Allow advertisement campaigns to target explicitly selected growth groups.

ALTER TABLE ad_campaign
    ADD COLUMN IF NOT EXISTS target_group_ids TEXT;

COMMENT ON COLUMN ad_campaign.target_group_ids
    IS 'JSON array of group table IDs; empty uses target_group_levels';
