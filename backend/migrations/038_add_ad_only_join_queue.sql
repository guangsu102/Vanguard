ALTER TABLE group_ad_handover
    ADD COLUMN IF NOT EXISTS batch_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS queue_position INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS join_interval_min_minutes INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS join_interval_max_minutes INTEGER NOT NULL DEFAULT 30,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS campaign_previous_json TEXT;

CREATE INDEX IF NOT EXISTS idx_group_ad_handover_join_queue
    ON group_ad_handover (workflow_type, status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_group_ad_handover_account_queue
    ON group_ad_handover (target_ad_only_account_id, status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_group_ad_handover_batch
    ON group_ad_handover (batch_id, queue_position);
