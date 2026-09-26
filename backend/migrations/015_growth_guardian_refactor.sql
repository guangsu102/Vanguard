-- Growth center / guardian center refactor

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'accounttype') THEN
        CREATE TYPE accounttype AS ENUM ('PROMOTER', 'GUARDIAN_BOT');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'guardianbothealthstatus') THEN
        CREATE TYPE guardianbothealthstatus AS ENUM ('UNKNOWN', 'HEALTHY', 'DEGRADED', 'OFFLINE');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'managedgroupbindingstatus') THEN
        CREATE TYPE managedgroupbindingstatus AS ENUM ('PENDING', 'ACTIVE', 'DEGRADED', 'INACTIVE');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'managedgroupbotrole') THEN
        CREATE TYPE managedgroupbotrole AS ENUM ('MEMBER', 'ADMIN', 'OWNER');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sensitivekeywordsource') THEN
        CREATE TYPE sensitivekeywordsource AS ENUM ('MANUAL', 'AI_SUGGESTION', 'VIOLATION_FEEDBACK');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'campaignscope') THEN
        CREATE TYPE campaignscope AS ENUM ('GLOBAL', 'MANAGED_GROUP');
    END IF;
END $$;

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS account_type accounttype NOT NULL DEFAULT 'PROMOTER',
    ADD COLUMN IF NOT EXISTS identifier VARCHAR(120),
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(120);

UPDATE telegram_account
SET identifier = COALESCE(identifier, phone, session_name)
WHERE identifier IS NULL;

ALTER TABLE telegram_account
    ALTER COLUMN identifier SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'ix_telegram_account_identifier'
    ) THEN
        CREATE UNIQUE INDEX ix_telegram_account_identifier ON telegram_account(identifier);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS guardian_bot_profile (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE REFERENCES telegram_account(id) ON DELETE CASCADE,
    bot_token VARCHAR(255) NOT NULL,
    bot_username VARCHAR(120),
    bot_user_id BIGINT,
    health_status guardianbothealthstatus NOT NULL DEFAULT 'UNKNOWN',
    sync_status VARCHAR(30) NOT NULL DEFAULT 'pending',
    permissions_snapshot TEXT,
    last_heartbeat_at TIMESTAMP,
    last_synced_at TIMESTAMP,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS moderation_sensitive_keyword (
    id SERIAL PRIMARY KEY,
    text VARCHAR(255) NOT NULL,
    normalized_text VARCHAR(255) NOT NULL,
    category VARCHAR(50) NOT NULL DEFAULT 'sensitive',
    source sensitivekeywordsource NOT NULL DEFAULT 'MANUAL',
    level violationlevel NOT NULL DEFAULT 'MEDIUM',
    action violationaction NOT NULL DEFAULT 'WARN',
    group_id BIGINT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1,
    source_sample TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_moderation_sensitive_keyword_text_group UNIQUE(normalized_text, group_id)
);

CREATE TABLE IF NOT EXISTS managed_group_binding (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL UNIQUE REFERENCES "group"(id) ON DELETE CASCADE,
    telegram_group_id BIGINT NOT NULL,
    bot_account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    binding_status managedgroupbindingstatus NOT NULL DEFAULT 'PENDING',
    bot_role managedgroupbotrole NOT NULL DEFAULT 'MEMBER',
    permissions_snapshot TEXT,
    bound_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_synced_at TIMESTAMP
);

ALTER TABLE group_verification_config
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS auto_kick_unverified BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS kick_after_minutes INTEGER NOT NULL DEFAULT 10;

CREATE TABLE IF NOT EXISTS group_moderation_policy (
    id SERIAL PRIMARY KEY,
    group_id BIGINT UNIQUE,
    message_interval_seconds INTEGER NOT NULL DEFAULT 10,
    max_messages_per_minute INTEGER NOT NULL DEFAULT 5,
    max_links_per_hour INTEGER NOT NULL DEFAULT 3,
    new_member_silent_minutes INTEGER NOT NULL DEFAULT 5,
    first_speak_delay_seconds INTEGER NOT NULL DEFAULT 30,
    media_policy TEXT,
    link_policy TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS group_punishment_policy (
    id SERIAL PRIMARY KEY,
    group_id BIGINT UNIQUE,
    warn_threshold INTEGER NOT NULL DEFAULT 3,
    mute_on_warn_threshold BOOLEAN NOT NULL DEFAULT TRUE,
    mute_duration_seconds INTEGER NOT NULL DEFAULT 300,
    ban_on_warn_threshold INTEGER NOT NULL DEFAULT 5,
    repeat_violation_window_hours INTEGER NOT NULL DEFAULT 24,
    auto_reset_warning_days INTEGER NOT NULL DEFAULT 7,
    severe_violation_direct_action violationaction NOT NULL DEFAULT 'MUTE',
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS acquisition_group_search_keyword (
    id SERIAL PRIMARY KEY,
    text VARCHAR(255) NOT NULL,
    keyword_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    source VARCHAR(20) NOT NULL DEFAULT 'manual',
    match_mode VARCHAR(20) NOT NULL DEFAULT 'fuzzy',
    trigger_count INTEGER NOT NULL DEFAULT 0,
    requires_review BOOLEAN NOT NULL DEFAULT TRUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_group_search_keyword_text_type UNIQUE(text, keyword_type)
);

ALTER TABLE campaign
    ADD COLUMN IF NOT EXISTS campaign_scope campaignscope NOT NULL DEFAULT 'GLOBAL',
    ADD COLUMN IF NOT EXISTS trigger_event VARCHAR(50),
    ADD COLUMN IF NOT EXISTS target_group_ids VARCHAR(500),
    ADD COLUMN IF NOT EXISTS bot_account_id INTEGER,
    ADD COLUMN IF NOT EXISTS distribution_mode VARCHAR(50),
    ADD COLUMN IF NOT EXISTS reward_policy_json VARCHAR(2000),
    ADD COLUMN IF NOT EXISTS broadcast_policy_json VARCHAR(2000),
    ADD COLUMN IF NOT EXISTS eligibility_policy_json VARCHAR(2000);
