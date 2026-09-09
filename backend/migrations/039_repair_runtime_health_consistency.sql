UPDATE group_account_membership
SET ad_status = 'blocked',
    updated_at = NOW()
WHERE status IN ('left', 'banned', 'rejected')
  AND ad_status IS DISTINCT FROM 'blocked';

CREATE INDEX IF NOT EXISTS idx_acquisition_conversation_context_expires
    ON acquisition_conversation_context (expires_at);

ALTER TABLE group_account_membership
    DROP CONSTRAINT IF EXISTS ck_group_membership_inactive_ad_blocked;

ALTER TABLE group_account_membership
    ADD CONSTRAINT ck_group_membership_inactive_ad_blocked
    CHECK (
        status NOT IN ('left', 'banned', 'rejected')
        OR ad_status = 'blocked'
    );
