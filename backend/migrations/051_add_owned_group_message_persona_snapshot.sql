-- Stage 3: freeze account Persona and prompt provenance on owned-group AI executions.
--
-- Operational prerequisite: both owned-group messaging switches must be off,
-- every producer/consumer must be drained, and generating/sending must be
-- empty.  The table lock keeps the precheck, backfill and final check inside
-- one stop-the-world transaction when this file is applied by the curated
-- migration runner.

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS persona_revision_snapshot INTEGER NULL;

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS persona_source_snapshot VARCHAR(32) NULL;

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS persona_snapshot JSONB NULL;

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS persona_hash VARCHAR(64) NULL;

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS prompt_template_version VARCHAR(32) NULL;

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS prompt_hash VARCHAR(64) NULL;

ALTER TABLE group_account_message_execution
    ADD COLUMN IF NOT EXISTS governance_rules_hash VARCHAR(64) NULL;

LOCK TABLE group_account_message_execution IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
    active_ids TEXT;
BEGIN
    SELECT string_agg(id::text, ',' ORDER BY id)
    INTO active_ids
    FROM (
        SELECT id
        FROM group_account_message_execution
        WHERE status IN ('generating', 'sending')
        ORDER BY id
        LIMIT 50
    ) AS active;

    IF active_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'stage3 persona migration requires generating/sending to be empty; execution_ids=%',
            active_ids;
    END IF;
END
$$;

DO $$
DECLARE
    invalid_ids TEXT;
BEGIN
    SELECT string_agg(id::text, ',' ORDER BY id)
    INTO invalid_ids
    FROM (
        SELECT execution.id
        FROM group_account_message_execution AS execution
        LEFT JOIN group_account_message_policy AS policy
            ON policy.id = execution.policy_id
        LEFT JOIN owned_group_assets AS asset
            ON asset.id = execution.owned_group_asset_id
        LEFT JOIN "group" AS core_group
            ON core_group.id = execution.core_group_id
        WHERE execution.mode_snapshot = 'ai'
          AND execution.status = 'queued'
          AND (
              policy.id IS NULL
              OR asset.id IS NULL
              OR core_group.id IS NULL
              OR execution.owned_group_asset_id <> policy.owned_group_asset_id
              OR policy.owned_group_asset_id <> asset.id
              OR execution.account_id <> policy.account_id
              OR execution.core_group_id <> policy.core_group_id
              OR policy.core_group_id <> asset.core_group_id
              OR asset.core_group_id <> core_group.id
              OR asset.telegram_chat_id IS NULL
              OR execution.telegram_chat_id <> asset.telegram_chat_id
              OR execution.telegram_chat_id <> core_group.group_id
              OR jsonb_typeof(policy.allowed_topics) <> 'array'
              OR EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(policy.allowed_topics) AS topic(value)
                  WHERE jsonb_typeof(topic.value) <> 'string'
              )
          )
        ORDER BY execution.id
        LIMIT 50
    ) AS invalid;

    IF invalid_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'stage3 persona queued execution mapping precheck failed; execution_ids=%',
            invalid_ids;
    END IF;
END
$$;

DO $$
DECLARE
    invalid_ids TEXT;
BEGIN
    SELECT string_agg(id::text, ',' ORDER BY id)
    INTO invalid_ids
    FROM (
        SELECT id
        FROM group_account_message_execution
        WHERE (
                mode_snapshot = 'template'
                AND (
                    persona_revision_snapshot IS NOT NULL
                    OR persona_source_snapshot IS NOT NULL
                    OR persona_snapshot IS NOT NULL
                    OR persona_hash IS NOT NULL
                    OR prompt_template_version IS NOT NULL
                    OR prompt_hash IS NOT NULL
                    OR governance_rules_hash IS NOT NULL
                )
            )
           OR (
                persona_source_snapshot IS NULL
                AND (
                    persona_revision_snapshot IS NOT NULL
                    OR persona_snapshot IS NOT NULL
                    OR persona_hash IS NOT NULL
                    OR prompt_template_version IS NOT NULL
                )
            )
           OR persona_source_snapshot NOT IN (
                'configured', 'neutral_default',
                'feature_disabled_default', 'legacy_default'
            )
           OR persona_revision_snapshot < 0
           OR (persona_hash IS NOT NULL AND persona_hash !~ '^[0-9a-f]{64}$')
           OR (prompt_hash IS NOT NULL AND prompt_hash !~ '^[0-9a-f]{64}$')
           OR (
                governance_rules_hash IS NOT NULL
                AND governance_rules_hash !~ '^[0-9a-f]{64}$'
            )
        ORDER BY id
        LIMIT 50
    ) AS invalid;

    IF invalid_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'stage3 persona execution metadata precheck failed; execution_ids=%',
            invalid_ids;
    END IF;
END
$$;

-- PostgreSQL provides Unicode normalization for UTF-8 databases.  The helper
-- mirrors the runtime snapshot sanitizer: NFKC, line-break collapsing,
-- control removal, whitespace collapsing and a hard character limit.
CREATE OR REPLACE FUNCTION pg_temp.vanguard_stage3_clean_text(input TEXT, max_chars INTEGER)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT LEFT(
        BTRIM(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            normalize(COALESCE(input, ''), NFKC),
                            E'[\\r\\n\\t]+',
                            ' ',
                            'g'
                        ),
                        '[[:cntrl:]]',
                        '',
                        'g'
                    ),
                    U&'[\200B-\200F\202A-\202E\2060\2066-\2069\FEFF]',
                    '',
                    'g'
                ),
                '[[:space:]]+',
                ' ',
                'g'
            )
        ),
        max_chars
    )
$$;

WITH queued_snapshots AS (
    SELECT
        execution.id AS execution_id,
        COALESCE(
            (
                SELECT jsonb_agg(limited.cleaned ORDER BY limited.ordinality)
                FROM (
                    SELECT deduplicated.cleaned, deduplicated.ordinality
                    FROM (
                        SELECT DISTINCT ON (lower(cleaned.cleaned))
                            cleaned.cleaned,
                            cleaned.ordinality
                        FROM (
                            SELECT
                                pg_temp.vanguard_stage3_clean_text(topic.value, 100) AS cleaned,
                                topic.ordinality
                            FROM jsonb_array_elements_text(policy.allowed_topics)
                                WITH ORDINALITY AS topic(value, ordinality)
                        ) AS cleaned
                        WHERE cleaned.cleaned <> ''
                        ORDER BY lower(cleaned.cleaned), cleaned.ordinality
                    ) AS deduplicated
                    ORDER BY deduplicated.ordinality
                    LIMIT 20
                ) AS limited
            ),
            '[]'::jsonb
        ) AS allowed_topics,
        pg_temp.vanguard_stage3_clean_text(core_group.title, 255) AS group_title
    FROM group_account_message_execution AS execution
    JOIN group_account_message_policy AS policy
        ON policy.id = execution.policy_id
    JOIN owned_group_assets AS asset
        ON asset.id = execution.owned_group_asset_id
    JOIN "group" AS core_group
        ON core_group.id = execution.core_group_id
    WHERE execution.mode_snapshot = 'ai'
      AND execution.status = 'queued'
      AND execution.persona_source_snapshot IS NULL
      AND execution.owned_group_asset_id = policy.owned_group_asset_id
      AND policy.owned_group_asset_id = asset.id
      AND execution.account_id = policy.account_id
      AND execution.core_group_id = policy.core_group_id
      AND policy.core_group_id = asset.core_group_id
      AND asset.core_group_id = core_group.id
      AND execution.telegram_chat_id = asset.telegram_chat_id
      AND execution.telegram_chat_id = core_group.group_id
)
UPDATE group_account_message_execution AS execution
SET persona_revision_snapshot = 0,
    persona_source_snapshot = 'legacy_default',
    persona_snapshot = '{"ad_style":"neutral","catchphrases":[],"expertise":[],"forbidden_topics":[],"interests":[],"language_style":"auto","name":"中性群友","preferred_topics":[],"reply_length":"short","schema_version":1,"system_prompt":"","tone":"自然、克制、简短"}'::jsonb,
    persona_hash = 'b8012c2a5d57294fbbe2041bb9a22dfa491146653b11e6c45a04b08c965db174',
    prompt_template_version = 'owned-group-neutral-legacy-v1',
    prompt_context = jsonb_set(
        COALESCE(execution.prompt_context, '{}'::jsonb),
        '{business_snapshot_v1}',
        jsonb_build_object(
            'allowed_topics', queued_snapshots.allowed_topics,
            'group_title', queued_snapshots.group_title
        ),
        TRUE
    )
FROM queued_snapshots
WHERE execution.id = queued_snapshots.execution_id;

UPDATE group_account_message_execution
SET persona_revision_snapshot = 0,
    persona_source_snapshot = 'legacy_default',
    persona_snapshot = '{"ad_style":"neutral","catchphrases":[],"expertise":[],"forbidden_topics":[],"interests":[],"language_style":"auto","name":"中性群友","preferred_topics":[],"reply_length":"short","schema_version":1,"system_prompt":"","tone":"自然、克制、简短"}'::jsonb,
    persona_hash = 'b8012c2a5d57294fbbe2041bb9a22dfa491146653b11e6c45a04b08c965db174',
    prompt_template_version = 'owned-group-neutral-legacy-v1'
WHERE mode_snapshot = 'ai'
  AND status IN ('pending_review', 'ready_to_send')
  AND persona_source_snapshot IS NULL;

DO $$
DECLARE
    missing_ids TEXT;
BEGIN
    SELECT string_agg(id::text, ',' ORDER BY id)
    INTO missing_ids
    FROM (
        SELECT id
        FROM group_account_message_execution
        WHERE mode_snapshot = 'ai'
          AND status = 'queued'
          AND (
              persona_revision_snapshot IS NULL
              OR persona_source_snapshot IS NULL
              OR persona_snapshot IS NULL
              OR persona_hash IS NULL
              OR prompt_template_version IS NULL
              OR prompt_context IS NULL
              OR NOT (prompt_context ? 'business_snapshot_v1')
          )
        ORDER BY id
        LIMIT 50
    ) AS missing;

    IF missing_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'stage3 persona queued execution final check failed; execution_ids=%',
            missing_ids;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_persona_source'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_persona_source
            CHECK (
                persona_source_snapshot IS NULL
                OR persona_source_snapshot IN (
                    'configured', 'neutral_default',
                    'feature_disabled_default', 'legacy_default'
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_persona_revision'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_persona_revision
            CHECK (persona_revision_snapshot IS NULL OR persona_revision_snapshot >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_template_persona_null'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_template_persona_null
            CHECK (
                mode_snapshot <> 'template'
                OR (
                    persona_revision_snapshot IS NULL
                    AND persona_source_snapshot IS NULL
                    AND persona_snapshot IS NULL
                    AND persona_hash IS NULL
                    AND prompt_template_version IS NULL
                    AND prompt_hash IS NULL
                    AND governance_rules_hash IS NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_ai_persona_required'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_ai_persona_required
            CHECK (
                mode_snapshot <> 'ai'
                OR status NOT IN ('queued', 'generating')
                OR (
                    persona_revision_snapshot IS NOT NULL
                    AND persona_source_snapshot IS NOT NULL
                    AND persona_snapshot IS NOT NULL
                    AND persona_hash IS NOT NULL
                    AND prompt_template_version IS NOT NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_persona_consistency'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_persona_consistency
            CHECK (
                persona_source_snapshot IS NOT NULL
                OR (
                    persona_revision_snapshot IS NULL
                    AND persona_snapshot IS NULL
                    AND persona_hash IS NULL
                    AND prompt_template_version IS NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_persona_hash_pg'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_persona_hash_pg
            CHECK (persona_hash IS NULL OR persona_hash ~ '^[0-9a-f]{64}$');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_prompt_hash_pg'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_prompt_hash_pg
            CHECK (prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'group_account_message_execution'::regclass
          AND conname = 'ck_group_account_message_execution_governance_hash_pg'
    ) THEN
        ALTER TABLE group_account_message_execution
            ADD CONSTRAINT ck_group_account_message_execution_governance_hash_pg
            CHECK (
                governance_rules_hash IS NULL
                OR governance_rules_hash ~ '^[0-9a-f]{64}$'
            );
    END IF;
END
$$;
