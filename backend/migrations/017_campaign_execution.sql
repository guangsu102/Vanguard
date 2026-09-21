-- Campaign execution state for delayed, scheduled, manual, and periodic campaigns.

CREATE TABLE IF NOT EXISTS campaign_execution (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
    group_id BIGINT,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    trigger_timing VARCHAR(30),
    trigger_event VARCHAR(50),
    distribution_mode VARCHAR(30),
    scheduled_at TIMESTAMP,
    executed_at TIMESTAMP,
    last_run_at TIMESTAMP,
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    reward_granted BOOLEAN NOT NULL DEFAULT FALSE,
    error VARCHAR(1000),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_campaign_execution_campaign
    ON campaign_execution(campaign_id);

CREATE INDEX IF NOT EXISTS idx_campaign_execution_user
    ON campaign_execution(user_id);

CREATE INDEX IF NOT EXISTS idx_campaign_execution_status_scheduled
    ON campaign_execution(status, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_campaign_execution_last_run
    ON campaign_execution(campaign_id, last_run_at);
