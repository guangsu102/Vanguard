-- Advertisement delivery is governed by its dedicated scheduler, throttle,
-- campaign, and group-capacity controls.  Remove the obsolete 100000/0
-- per-account risk-budget entry from any older persisted settings.
UPDATE system_setting
SET value = jsonb_set(
        COALESCE(NULLIF(BTRIM(value), '')::jsonb, '{}'::jsonb),
        '{actions}',
        COALESCE(
            COALESCE(NULLIF(BTRIM(value), '')::jsonb, '{}'::jsonb)->'actions',
            '{}'::jsonb
        ) - 'ad_delivery',
        true
    )::text
WHERE key = 'automation.account_risk_guard'
  AND value IS NOT NULL
  AND NULLIF(BTRIM(value), '')::jsonb ? 'actions';
