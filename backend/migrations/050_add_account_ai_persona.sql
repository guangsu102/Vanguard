-- Stage 3: account-level AI Persona.
--
-- This migration is intentionally replay-safe. Existing app.runtime_settings
-- content is never rewritten: a seed row is inserted only when the key does
-- not already exist.

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS ai_persona JSONB NULL;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS ai_persona_revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS ai_persona_hash VARCHAR(64) NULL;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS ai_persona_updated_at TIMESTAMP WITHOUT TIME ZONE NULL;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS ai_persona_updated_by INTEGER NULL;

ALTER TABLE telegram_account
    ALTER COLUMN ai_persona_revision SET DEFAULT 0;

UPDATE telegram_account
SET ai_persona_revision = 0
WHERE ai_persona_revision IS NULL;

ALTER TABLE telegram_account
    ALTER COLUMN ai_persona_revision SET NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM telegram_account
        WHERE ai_persona_revision < 0
           OR (ai_persona IS NULL AND ai_persona_hash IS NOT NULL)
           OR (
               ai_persona IS NOT NULL
               AND (
                   ai_persona_revision < 1
                   OR ai_persona_hash IS NULL
                   OR ai_persona_hash !~ '^[0-9a-f]{64}$'
               )
           )
    ) THEN
        RAISE EXCEPTION
            'telegram_account contains inconsistent ai_persona metadata';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'telegram_account'::regclass
          AND conname = 'ck_telegram_account_account_persona_revision_non_negative'
    ) THEN
        ALTER TABLE telegram_account
            ADD CONSTRAINT ck_telegram_account_account_persona_revision_non_negative
            CHECK (ai_persona_revision >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'telegram_account'::regclass
          AND conname = 'ck_telegram_account_account_persona_consistency_postgresql'
    ) THEN
        ALTER TABLE telegram_account
            ADD CONSTRAINT ck_telegram_account_account_persona_consistency_postgresql
            CHECK (
                (ai_persona IS NULL AND ai_persona_hash IS NULL)
                OR (
                    ai_persona IS NOT NULL
                    AND ai_persona_revision >= 1
                    AND ai_persona_hash ~ '^[0-9a-f]{64}$'
                )
            );
    END IF;
END
$$;

INSERT INTO system_setting (key, value, description)
VALUES (
    'app.runtime_settings',
    '{"ownedGroupAiPersona":{"enabled":false,"revision":0,"updatedAt":null,"updatedBy":null}}',
    'Admin-managed runtime application settings'
)
ON CONFLICT (key) DO NOTHING;
