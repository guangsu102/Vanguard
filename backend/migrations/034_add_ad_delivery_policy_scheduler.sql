ALTER TABLE ad_campaign
    ADD COLUMN IF NOT EXISTS delivery_policy VARCHAR(20) NOT NULL DEFAULT 'growth';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_ad_campaign_delivery_policy'
          AND conrelid = 'ad_campaign'::regclass
    ) THEN
        ALTER TABLE ad_campaign
            ADD CONSTRAINT ck_ad_campaign_delivery_policy
            CHECK (delivery_policy IN ('growth', 'ad_only'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_ad_campaign_delivery_policy
    ON ad_campaign (delivery_policy);

ALTER TABLE telegram_account_operation_config
    ALTER COLUMN max_messages_per_day DROP NOT NULL;

CREATE TABLE IF NOT EXISTS ad_delivery_schedule_state (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES ad_campaign(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
    telegram_group_id BIGINT NOT NULL,
    next_due_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'idle',
    lock_token VARCHAR(64),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    last_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    last_success_at TIMESTAMP WITHOUT TIME ZONE,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_reason VARCHAR(255),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ad_delivery_schedule_tuple
        UNIQUE (campaign_id, account_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_ad_delivery_schedule_due
    ON ad_delivery_schedule_state (status, next_due_at);

CREATE INDEX IF NOT EXISTS idx_ad_delivery_schedule_account
    ON ad_delivery_schedule_state (account_id, next_due_at);

CREATE INDEX IF NOT EXISTS idx_ad_delivery_schedule_lease
    ON ad_delivery_schedule_state (lease_expires_at);

COMMENT ON COLUMN ad_campaign.delivery_policy
    IS 'Advertisement delivery policy: growth or ad_only';
