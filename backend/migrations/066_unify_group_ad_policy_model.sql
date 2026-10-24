-- Use the shared production LLM route for group advertising-policy reviews.
-- Older deployments persisted the temporary Ark Coding Plan model. Some
-- records use the API's snake_case key while older frontend payloads used the
-- camelCase spelling, so normalize either known legacy value and preserve a
-- custom operator-selected model.
WITH normalized AS (
    SELECT
        key,
        value,
        COALESCE(NULLIF(BTRIM(value), '')::jsonb, '{}'::jsonb) AS payload
    FROM system_setting
    WHERE key = 'automation.ad_capacity'
)
UPDATE system_setting AS setting
SET value = jsonb_set(
        normalized.payload - 'adPolicyAiModel',
        '{ad_policy_ai_model}',
        to_jsonb('gpt-6.1-sol'::text),
        true
    )::text,
    updated_at = NOW()
FROM normalized
WHERE setting.key = normalized.key
  AND setting.value IS NOT DISTINCT FROM normalized.value
  AND COALESCE(
        normalized.payload->>'ad_policy_ai_model',
        normalized.payload->>'adPolicyAiModel'
      ) IN ('glm-5.3-flash', 'gpt-6-sol', 'gpt-5.6-sol');
