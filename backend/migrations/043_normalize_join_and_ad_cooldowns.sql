ALTER TABLE telegram_account_operation_config
    ALTER COLUMN max_groups_per_day SET DEFAULT 10;

UPDATE telegram_account_operation_config
SET max_groups_per_day = 10
WHERE max_groups_per_day > 10;

INSERT INTO system_setting (key, value, description)
VALUES (
    'automation.account_risk_guard',
    '{"actions":{"join":{"daily_limit":10,"cooldown_seconds":7200}}}',
    'Account risk guard budgets and cooldowns'
)
ON CONFLICT (key) DO UPDATE
SET value = jsonb_set(
        COALESCE(NULLIF(BTRIM(system_setting.value), ''), '{}')::jsonb,
        '{actions}',
        COALESCE(
            COALESCE(NULLIF(BTRIM(system_setting.value), ''), '{}')::jsonb -> 'actions',
            '{}'::jsonb
        ) || jsonb_build_object(
            'join',
            COALESCE(
                COALESCE(NULLIF(BTRIM(system_setting.value), ''), '{}')::jsonb
                    -> 'actions' -> 'join',
                '{}'::jsonb
            ) || '{"daily_limit":10,"cooldown_seconds":7200}'::jsonb
        ),
        TRUE
    )::text,
    description = EXCLUDED.description,
    updated_at = NOW();

INSERT INTO system_setting (key, value, description)
VALUES (
    'automation.ad_delivery_execution',
    '{"growth_group_global_cooldown_seconds":86400}',
    'Advertisement delivery execution settings'
)
ON CONFLICT (key) DO UPDATE
SET value = jsonb_set(
        COALESCE(NULLIF(BTRIM(system_setting.value), ''), '{}')::jsonb,
        '{growth_group_global_cooldown_seconds}',
        '86400'::jsonb,
        TRUE
    )::text,
    description = EXCLUDED.description,
    updated_at = NOW();
