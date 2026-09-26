-- Advertisement warmup/probe state for dynamic delivery control.

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS warmup_status VARCHAR(40) NOT NULL DEFAULT 'joined_pending_test';

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS probe_status VARCHAR(40) NOT NULL DEFAULT 'not_started';

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS probe_due_at TIMESTAMP;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS last_probe_at TIMESTAMP;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS ad_eligible_after TIMESTAMP;

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS last_probe_error TEXT;

CREATE INDEX IF NOT EXISTS idx_group_membership_warmup
    ON group_account_membership(account_id, warmup_status, probe_status);

CREATE INDEX IF NOT EXISTS idx_group_membership_ad_eligible
    ON group_account_membership(account_id, ad_eligible_after);

UPDATE group_account_membership
SET warmup_status = CASE
        WHEN status != 'joined' THEN 'blocked'
        WHEN note LIKE '%ad_probe_success%' THEN 'writable_verified'
        WHEN note LIKE '%ad_probe_failed%' THEN 'blocked'
        WHEN note LIKE '%ad_group_control_leave%' THEN 'blocked'
        ELSE warmup_status
    END,
    probe_status = CASE
        WHEN note LIKE '%ad_probe_success%' THEN 'success'
        WHEN note LIKE '%ad_probe_failed%' THEN 'failed'
        WHEN note LIKE '%ad_probe_due%' THEN 'scheduled'
        ELSE probe_status
    END
WHERE warmup_status = 'joined_pending_test'
   OR probe_status = 'not_started';

COMMENT ON COLUMN group_account_membership.warmup_status
    IS 'Advertisement warmup state: joined_pending_test/probe_scheduled/writable_verified/ad_eligible/blocked/ad_delivered';

COMMENT ON COLUMN group_account_membership.probe_status
    IS 'Soft-ad probe status: not_started/scheduled/success/failed/skipped';

COMMENT ON COLUMN group_account_membership.probe_due_at
    IS 'Scheduled probe-message time';

COMMENT ON COLUMN group_account_membership.last_probe_at
    IS 'Last probe-message attempt time';

COMMENT ON COLUMN group_account_membership.ad_eligible_after
    IS 'Soft-ad eligibility time after successful warmup';

COMMENT ON COLUMN group_account_membership.last_probe_error
    IS 'Last probe-message error';
