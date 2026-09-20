ALTER TABLE ad_campaign
    ALTER COLUMN max_sends_per_account_per_day SET DEFAULT 30;

ALTER TABLE telegram_account_operation_config
    ALTER COLUMN max_groups_total SET DEFAULT 100;

UPDATE ad_campaign
SET max_sends_per_group_per_day = 1,
    max_sends_per_account_per_day = 30,
    updated_at = NOW()
WHERE delivery_policy = 'growth';

UPDATE telegram_account_operation_config
SET max_groups_total = LEAST(max_groups_total, 100),
    max_messages_per_day = LEAST(COALESCE(max_messages_per_day, 30), 30),
    updated_at = NOW()
WHERE operation_mode = 'growth';

INSERT INTO system_setting (key, value, description)
VALUES (
    'automation.ad_delivery_throttle',
    '{"enabled": true, "growth_min_interval_seconds": 600, "growth_max_interval_seconds": 1800}',
    'Advertisement delivery throttle settings'
)
ON CONFLICT (key) DO UPDATE
SET value = (
        COALESCE(NULLIF(system_setting.value, ''), '{}')::jsonb
        || EXCLUDED.value::jsonb
    )::text,
    description = EXCLUDED.description,
    updated_at = NOW();

INSERT INTO system_setting (key, value, description)
VALUES (
    'automation.ad_capacity',
    '{"max_groups_per_account": 100}',
    'Advertisement capacity and survival settings'
)
ON CONFLICT (key) DO UPDATE
SET value = (
        COALESCE(NULLIF(system_setting.value, ''), '{}')::jsonb
        || EXCLUDED.value::jsonb
    )::text,
    description = EXCLUDED.description,
    updated_at = NOW();

INSERT INTO system_setting (key, value, description)
VALUES (
    'automation.account_risk_guard',
    '{"account_outbound_message_hard_cap_default": 30}',
    'Account risk guard budgets and cooldowns'
)
ON CONFLICT (key) DO UPDATE
SET value = (
        COALESCE(NULLIF(system_setting.value, ''), '{}')::jsonb
        || EXCLUDED.value::jsonb
    )::text,
    description = EXCLUDED.description,
    updated_at = NOW();
