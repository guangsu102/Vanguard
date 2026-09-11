-- Link self-owned Telegram groups to their explicit Guardian governance state.
-- Existing assets stay disabled and no core group or binding is created here.
ALTER TABLE owned_group_assets
    ADD COLUMN IF NOT EXISTS core_group_id INTEGER,
    ADD COLUMN IF NOT EXISTS managed_binding_id INTEGER,
    ADD COLUMN IF NOT EXISTS guardian_bot_account_id INTEGER,
    ADD COLUMN IF NOT EXISTS governance_status VARCHAR(32) DEFAULT 'disabled',
    ADD COLUMN IF NOT EXISTS governance_pending_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS governance_enabled_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS governance_last_checked_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS governance_last_error_code VARCHAR(64),
    ADD COLUMN IF NOT EXISTS governance_last_error_message TEXT;

UPDATE owned_group_assets
SET governance_status = 'disabled'
WHERE governance_status IS NULL
   OR governance_status NOT IN ('disabled', 'pending', 'managed', 'degraded');

ALTER TABLE owned_group_assets
    ALTER COLUMN governance_status SET DEFAULT 'disabled',
    ALTER COLUMN governance_status SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_owned_group_assets_core_group'
          AND conrelid = 'owned_group_assets'::regclass
    ) THEN
        ALTER TABLE owned_group_assets
            ADD CONSTRAINT fk_owned_group_assets_core_group
            FOREIGN KEY (core_group_id)
            REFERENCES "group"(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_owned_group_assets_managed_binding'
          AND conrelid = 'owned_group_assets'::regclass
    ) THEN
        ALTER TABLE owned_group_assets
            ADD CONSTRAINT fk_owned_group_assets_managed_binding
            FOREIGN KEY (managed_binding_id)
            REFERENCES managed_group_binding(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_owned_group_assets_guardian_bot_account'
          AND conrelid = 'owned_group_assets'::regclass
    ) THEN
        ALTER TABLE owned_group_assets
            ADD CONSTRAINT fk_owned_group_assets_guardian_bot_account
            FOREIGN KEY (guardian_bot_account_id)
            REFERENCES telegram_account(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_owned_group_assets_governance_status'
          AND conrelid = 'owned_group_assets'::regclass
    ) THEN
        ALTER TABLE owned_group_assets
            ADD CONSTRAINT ck_owned_group_assets_governance_status
            CHECK (governance_status IN ('disabled', 'pending', 'managed', 'degraded'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_owned_group_assets_core_group
    ON owned_group_assets (core_group_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owned_group_assets_managed_binding
    ON owned_group_assets (managed_binding_id);

CREATE INDEX IF NOT EXISTS idx_owned_group_assets_guardian_status
    ON owned_group_assets (guardian_bot_account_id, governance_status);

COMMENT ON COLUMN owned_group_assets.core_group_id
    IS 'Internal group.id used by Guardian policies and records';
COMMENT ON COLUMN owned_group_assets.managed_binding_id
    IS 'Explicit primary Guardian binding for this self-owned group';
COMMENT ON COLUMN owned_group_assets.guardian_bot_account_id
    IS 'telegram_account.id of the selected Guardian Bot';
COMMENT ON COLUMN owned_group_assets.governance_status
    IS 'Independent governance state: disabled, pending, managed, or degraded';
