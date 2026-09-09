-- Keep one user-configurable automatic-probe limit per account and remove the
-- retired global limit plus the duplicate editable account-risk budget.
UPDATE system_setting
SET value = (
        COALESCE(NULLIF(BTRIM(value), '')::jsonb, '{}'::jsonb)
            - 'ad_policy_auto_probe_daily_limit'
            - 'adPolicyAutoProbeDailyLimit'
    )::text
WHERE key = 'automation.ad_capacity'
  AND value IS NOT NULL
  AND NULLIF(BTRIM(value), '') IS NOT NULL;

UPDATE system_setting
SET value = jsonb_set(
        COALESCE(NULLIF(BTRIM(value), '')::jsonb, '{}'::jsonb),
        '{actions}',
        COALESCE(
            COALESCE(NULLIF(BTRIM(value), '')::jsonb, '{}'::jsonb)->'actions',
            '{}'::jsonb
        ) - 'ad_probe' - 'adProbe',
        true
    )::text
WHERE key = 'automation.account_risk_guard'
  AND value IS NOT NULL
  AND NULLIF(BTRIM(value), '') IS NOT NULL;
