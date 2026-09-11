-- Stage 2: per-owned-group/per-account AI and template messaging.
-- This migration is additive and deliberately does not reinterpret historical
-- acquisition or advertisement messages as owned-group executions.

CREATE TABLE IF NOT EXISTS group_account_message_policy (
    id SERIAL PRIMARY KEY,
    owned_group_asset_id INTEGER NOT NULL REFERENCES owned_group_assets(id) ON DELETE CASCADE,
    core_group_id INTEGER NOT NULL REFERENCES "group"(id) ON DELETE RESTRICT,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE RESTRICT,
    mode VARCHAR(16) NOT NULL DEFAULT 'off',
    default_template_id INTEGER REFERENCES acquisition_message_template(id) ON DELETE RESTRICT,
    trigger_config JSONB NOT NULL DEFAULT '{"version":1,"scheduled":{"enabled":false,"timezone":"Asia/Shanghai","weekdays":[1,2,3,4,5,6,7],"times":[],"jitter_seconds":0,"content_category":"community"},"keyword":{"enabled":false,"trigger_ids":[],"reply_to_source":true,"content_category":"community"},"reply":{"enabled":false,"strategy":"directed","semantic_min_confidence":0.75,"context_messages":6,"content_category":"community"},"manual":{"enabled":true,"allowed_content_categories":["community","promotion"]},"dedupe_window_seconds":21600}'::jsonb,
    promotion_config JSONB NOT NULL DEFAULT '{"mode":"off","default_template_id":null,"destination_url":null,"cta_text":null}'::jsonb,
    daily_limit INTEGER NOT NULL DEFAULT 5,
    cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
    allowed_topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    require_review BOOLEAN NOT NULL DEFAULT TRUE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    updated_by INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_group_account_message_policy_asset_account
        UNIQUE (owned_group_asset_id, account_id),
    CONSTRAINT ck_group_account_message_policy_message_policy_mode
        CHECK (mode IN ('ai', 'template', 'off')),
    CONSTRAINT ck_group_account_message_policy_message_policy_daily_limit
        CHECK (daily_limit BETWEEN 0 AND 100),
    CONSTRAINT ck_group_account_message_policy_message_policy_cooldown_seconds
        CHECK (cooldown_seconds BETWEEN 60 AND 86400),
    CONSTRAINT ck_group_account_message_policy_message_policy_revision
        CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS idx_message_policy_group_enabled
    ON group_account_message_policy (core_group_id, enabled);
CREATE INDEX IF NOT EXISTS idx_message_policy_account_enabled
    ON group_account_message_policy (account_id, enabled);

CREATE TABLE IF NOT EXISTS group_account_message_execution (
    id BIGSERIAL PRIMARY KEY,
    policy_id INTEGER NOT NULL REFERENCES group_account_message_policy(id) ON DELETE RESTRICT,
    owned_group_asset_id INTEGER NOT NULL,
    core_group_id INTEGER NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    account_id INTEGER NOT NULL,
    trigger_type VARCHAR(16) NOT NULL,
    message_purpose VARCHAR(32) NOT NULL,
    content_category VARCHAR(16) NOT NULL,
    mode_snapshot VARCHAR(16) NOT NULL,
    policy_revision INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    source_message_id BIGINT,
    reply_to_message_id BIGINT,
    keyword_trigger_id INTEGER REFERENCES acquisition_keyword_trigger(id) ON DELETE SET NULL,
    template_id INTEGER REFERENCES acquisition_message_template(id) ON DELETE SET NULL,
    topic VARCHAR(100),
    prompt_context JSONB,
    promotion_config_snapshot JSONB,
    content TEXT,
    content_hash VARCHAR(64),
    idempotency_key VARCHAR(128) NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    scheduled_at TIMESTAMP WITHOUT TIME ZONE,
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    lease_id VARCHAR(128),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    write_started_at TIMESTAMP WITHOUT TIME ZONE,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    requested_by INTEGER,
    reviewer_id INTEGER,
    reviewed_at TIMESTAMP WITHOUT TIME ZONE,
    telegram_message_id BIGINT,
    error_code VARCHAR(64),
    error_message TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT uq_group_account_message_execution_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_group_account_message_execution_message_execution_trigger_type
        CHECK (trigger_type IN ('scheduled', 'keyword', 'reply', 'manual')),
    CONSTRAINT ck_group_account_message_execution_message_execution_purpose
        CHECK (message_purpose IN ('community_ai', 'template')),
    CONSTRAINT ck_group_account_message_execution_message_execution_category
        CHECK (content_category IN ('community', 'promotion')),
    CONSTRAINT ck_group_account_message_execution_message_execution_mode
        CHECK (mode_snapshot IN ('ai', 'template')),
    CONSTRAINT ck_group_account_message_execution_message_execution_status
        CHECK (status IN ('queued', 'generating', 'pending_review', 'ready_to_send',
            'sending', 'sent', 'skipped', 'failed', 'rejected', 'expired', 'cancelled')),
    CONSTRAINT ck_group_account_message_execution_message_execution_attempt_count
        CHECK (attempt_count BETWEEN 0 AND 3),
    CONSTRAINT ck_group_account_message_execution_message_execution_revision
        CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS idx_message_execution_dispatch
    ON group_account_message_execution (status, scheduled_at, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_message_execution_policy_created
    ON group_account_message_execution (policy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_execution_group_sent
    ON group_account_message_execution (core_group_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_message_execution_group_category_sent
    ON group_account_message_execution (core_group_id, content_category, sent_at);
CREATE INDEX IF NOT EXISTS idx_message_execution_account_sent
    ON group_account_message_execution (account_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_message_execution_group_hash_created
    ON group_account_message_execution (core_group_id, content_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_message_execution_asset_source
    ON group_account_message_execution (owned_group_asset_id, source_message_id);

ALTER TABLE acquisition_message_template
    ADD COLUMN IF NOT EXISTS scope VARCHAR(16) DEFAULT 'acquisition',
    ADD COLUMN IF NOT EXISTS owned_group_asset_id INTEGER;

UPDATE acquisition_message_template
SET scope = 'acquisition', owned_group_asset_id = NULL
WHERE scope IS NULL;

ALTER TABLE acquisition_message_template
    ALTER COLUMN scope SET DEFAULT 'acquisition',
    ALTER COLUMN scope SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_acquisition_message_template_owned_group_asset_id_owned_group_assets'
          AND conrelid = 'acquisition_message_template'::regclass
    ) THEN
        ALTER TABLE acquisition_message_template
            ADD CONSTRAINT fk_acquisition_message_template_owned_group_asset_id_owned_group_assets
            FOREIGN KEY (owned_group_asset_id)
            REFERENCES owned_group_assets(id)
            ON DELETE CASCADE;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_acquisition_message_template_message_template_scope'
          AND conrelid = 'acquisition_message_template'::regclass
    ) THEN
        ALTER TABLE acquisition_message_template
            ADD CONSTRAINT ck_acquisition_message_template_message_template_scope
            CHECK (scope IN ('acquisition', 'owned_group'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_acquisition_message_template_message_template_scope_asset'
          AND conrelid = 'acquisition_message_template'::regclass
    ) THEN
        ALTER TABLE acquisition_message_template
            ADD CONSTRAINT ck_acquisition_message_template_message_template_scope_asset
            CHECK (
                (scope = 'acquisition' AND owned_group_asset_id IS NULL)
                OR (scope = 'owned_group' AND owned_group_asset_id IS NOT NULL)
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_message_template_scope_asset_enabled
    ON acquisition_message_template (scope, owned_group_asset_id, enabled);

ALTER TABLE acquisition_message
    ADD COLUMN IF NOT EXISTS message_purpose VARCHAR(32),
    ADD COLUMN IF NOT EXISTS content_category VARCHAR(16),
    ADD COLUMN IF NOT EXISTS owned_group_execution_id BIGINT,
    ADD COLUMN IF NOT EXISTS core_group_id INTEGER,
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_acquisition_message_owned_group_execution_id_group_account_message_execution'
          AND conrelid = 'acquisition_message'::regclass
    ) THEN
        ALTER TABLE acquisition_message
            ADD CONSTRAINT fk_acquisition_message_owned_group_execution_id_group_account_message_execution
            FOREIGN KEY (owned_group_execution_id)
            REFERENCES group_account_message_execution(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_acquisition_message_core_group_id_group'
          AND conrelid = 'acquisition_message'::regclass
    ) THEN
        ALTER TABLE acquisition_message
            ADD CONSTRAINT fk_acquisition_message_core_group_id_group
            FOREIGN KEY (core_group_id)
            REFERENCES "group"(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_acquisition_message_owned_group_execution_id'
          AND conrelid = 'acquisition_message'::regclass
    ) THEN
        ALTER TABLE acquisition_message
            ADD CONSTRAINT uq_acquisition_message_owned_group_execution_id
            UNIQUE (owned_group_execution_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_msg_core_group_sent
    ON acquisition_message (core_group_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_msg_content_hash
    ON acquisition_message (core_group_id, content_hash);

COMMENT ON TABLE group_account_message_policy
    IS 'Per-owned-group/per-promoter messaging policy; account_id is immutable';
COMMENT ON TABLE group_account_message_execution
    IS 'Frozen owned-group message execution, review and delivery state';
COMMENT ON COLUMN acquisition_message.group_id
    IS 'Telegram Chat ID; core_group_id is the internal group foreign key';
