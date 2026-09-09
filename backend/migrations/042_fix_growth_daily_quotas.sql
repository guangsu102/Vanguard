ALTER TABLE acquisition_auto_join_attempt
    ADD COLUMN IF NOT EXISTS telegram_action_attempted BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE acquisition_auto_join_attempt
SET telegram_action_attempted = TRUE
WHERE telegram_action_attempted = FALSE
  AND (
      status = 'success'
      OR joined_at IS NOT NULL
      OR (status = 'pending' AND reason = 'join_request_pending')
  );

CREATE INDEX IF NOT EXISTS idx_auto_join_account_action_attempted
    ON acquisition_auto_join_attempt (
        account_id,
        telegram_action_attempted,
        attempted_at
    );

ALTER TABLE ad_campaign
    ALTER COLUMN max_sends_per_account_per_day SET DEFAULT 10;
