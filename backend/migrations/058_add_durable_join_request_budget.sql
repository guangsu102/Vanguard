ALTER TABLE acquisition_auto_join_attempt
    ADD COLUMN IF NOT EXISTS request_state VARCHAR(30) NOT NULL DEFAULT 'released',
    ADD COLUMN IF NOT EXISTS reservation_key VARCHAR(64),
    ADD COLUMN IF NOT EXISTS target_key VARCHAR(500),
    ADD COLUMN IF NOT EXISTS require_auto_join_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS reservation_expires_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS request_sent_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS reservation_released_at TIMESTAMP WITHOUT TIME ZONE;

UPDATE acquisition_auto_join_attempt
SET request_state = CASE
        WHEN telegram_action_attempted THEN 'sent'
        ELSE 'released'
    END,
    request_sent_at = CASE
        WHEN telegram_action_attempted THEN attempted_at
        ELSE NULL
    END
WHERE request_sent_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_join_reservation_key
    ON acquisition_auto_join_attempt (reservation_key);

CREATE INDEX IF NOT EXISTS idx_auto_join_account_request_state
    ON acquisition_auto_join_attempt (
        account_id,
        request_state,
        request_sent_at
    );

CREATE INDEX IF NOT EXISTS idx_auto_join_account_target_state
    ON acquisition_auto_join_attempt (account_id, target_key, request_state);

ALTER TABLE telegram_account_operation_config
    ADD COLUMN IF NOT EXISTS join_review_backlog_paused BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS join_review_backlog_reason VARCHAR(255);

ALTER TABLE group_account_membership
    ADD COLUMN IF NOT EXISTS review_status VARCHAR(40) NOT NULL DEFAULT 'initial_pending',
    ADD COLUMN IF NOT EXISTS review_started_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS review_next_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS review_deadline_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS review_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS leave_requested_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS leave_confirmed_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS leave_retry_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS leave_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS leave_error TEXT;

ALTER TABLE group_account_membership
    DROP CONSTRAINT IF EXISTS ck_group_membership_inactive_ad_blocked;

ALTER TABLE group_account_membership
    ADD CONSTRAINT ck_group_membership_inactive_ad_blocked
    CHECK (
        status NOT IN ('left', 'banned', 'rejected', 'leave_failed')
        OR ad_status = 'blocked'
    );

UPDATE group_account_membership
SET review_started_at = COALESCE(review_started_at, joined_at, created_at),
    review_deadline_at = COALESCE(
        review_deadline_at,
        COALESCE(joined_at, created_at) + INTERVAL '24 hours'
    ),
    review_next_at = COALESCE(
        review_next_at,
        CASE
            WHEN status = 'rejected' THEN NOW()
            ELSE COALESCE(joined_at, created_at) + INTERVAL '2 hours'
        END
    ),
    review_status = CASE
        WHEN status = 'rejected' THEN 'leave_failed'
        WHEN status IN ('left', 'banned') THEN 'left'
        WHEN status = 'joined' AND ad_status = 'active' THEN 'approved'
        ELSE 'initial_pending'
    END;

CREATE INDEX IF NOT EXISTS idx_group_membership_review_due
    ON group_account_membership (account_id, review_status, review_next_at);

ALTER TABLE telegram_account_operation_config
    ALTER COLUMN max_groups_per_day SET DEFAULT 30,
    ALTER COLUMN join_interval_min_seconds SET DEFAULT 2880,
    ALTER COLUMN join_interval_max_seconds SET DEFAULT 7200;

UPDATE telegram_account_operation_config
SET max_groups_per_day = CASE
        WHEN max_groups_per_day = 0 THEN 0
        ELSE 30
    END,
    join_interval_min_seconds = GREATEST(join_interval_min_seconds, 2880),
    join_interval_max_seconds = GREATEST(
        join_interval_max_seconds,
        join_interval_min_seconds,
        2880
    ),
    updated_at = NOW();

ALTER TABLE group_ad_handover
    ALTER COLUMN join_interval_min_minutes SET DEFAULT 48,
    ALTER COLUMN join_interval_max_minutes SET DEFAULT 120;

UPDATE group_ad_handover
SET join_interval_min_minutes = GREATEST(join_interval_min_minutes, 48),
    join_interval_max_minutes = GREATEST(
        join_interval_max_minutes,
        join_interval_min_minutes,
        48
    );

UPDATE system_setting
SET value = jsonb_set(value::jsonb, '{enabled}', 'false'::jsonb, TRUE)::text,
    updated_at = NOW()
WHERE key = 'automation.auto_join_scheduler';
