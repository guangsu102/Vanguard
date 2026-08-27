ALTER TABLE group_ad_handover
    ADD COLUMN IF NOT EXISTS workflow_type VARCHAR(20) NOT NULL DEFAULT 'assessment',
    ADD COLUMN IF NOT EXISTS permission_mode VARCHAR(40),
    ADD COLUMN IF NOT EXISTS permission_note TEXT,
    ADD COLUMN IF NOT EXISTS permission_expires_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS permission_previous_json TEXT,
    ADD COLUMN IF NOT EXISTS membership_previous_json TEXT;

ALTER TABLE group_ad_handover
    ALTER COLUMN assessment_id DROP NOT NULL,
    ALTER COLUMN group_id DROP NOT NULL,
    ALTER COLUMN source_growth_account_id DROP NOT NULL;

ALTER TABLE group_ad_only_event
    ALTER COLUMN group_id DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_group_ad_handover_workflow
    ON group_ad_handover (workflow_type, status, updated_at);
