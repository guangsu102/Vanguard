-- Expand first. Historical audits and non-growth outbound ledgers remain intact.
ALTER TABLE group_account_membership ADD COLUMN IF NOT EXISTS next_ad_allowed_at timestamp;
ALTER TABLE group_account_membership ADD COLUMN IF NOT EXISTS permission_checked_at timestamp;
ALTER TABLE group_account_membership ADD COLUMN IF NOT EXISTS can_send_text boolean;
ALTER TABLE group_account_membership ADD COLUMN IF NOT EXISTS permission_reason varchar(160);
ALTER TABLE group_account_membership ADD COLUMN IF NOT EXISTS verification_status varchar(24) NOT NULL DEFAULT 'pending';
CREATE INDEX IF NOT EXISTS idx_group_membership_ad_due
    ON group_account_membership(account_id, status, review_status, next_ad_allowed_at);

-- Preserve a human pause when a membership event reports departure. Both states
-- remain ineligible; older application versions remain compatible.
ALTER TABLE group_account_membership
    DROP CONSTRAINT IF EXISTS ck_group_membership_inactive_ad_blocked;
ALTER TABLE group_account_membership
    DROP CONSTRAINT IF EXISTS ck_group_account_membership_ck_group_membership_inactive_ad_blocked;
ALTER TABLE group_account_membership
    ADD CONSTRAINT ck_group_membership_inactive_ad_blocked
    CHECK (status NOT IN ('left', 'banned', 'rejected', 'leave_failed') OR ad_status IN ('blocked', 'paused'));

ALTER TABLE xboard_callback ADD COLUMN IF NOT EXISTS attributed_account_id integer
    REFERENCES telegram_account(id) ON DELETE SET NULL;
ALTER TABLE xboard_callback ADD COLUMN IF NOT EXISTS attribution_source varchar(40);
CREATE INDEX IF NOT EXISTS ix_xboard_callback_attributed_account_id
    ON xboard_callback(attributed_account_id);

-- This file only expands the schema. State conversion is explicitly performed
-- after old growth consumers have drained, by the idempotent activation script.
