ALTER TABLE group_ad_policy_event
    ADD COLUMN IF NOT EXISTS account_id INTEGER
    REFERENCES telegram_account(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_group_ad_policy_event_account
    ON group_ad_policy_event (account_id, created_at);

COMMENT ON COLUMN group_ad_policy_event.account_id IS 'Telegram account used for this policy probe or event';
