-- Acquisition automation, account operation settings, and advertisement delivery.

CREATE TABLE IF NOT EXISTS telegram_account_operation_config (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    auto_join_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    auto_ads_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    max_groups_per_day INTEGER NOT NULL DEFAULT 5,
    max_groups_total INTEGER NOT NULL DEFAULT 100,
    join_interval_min_seconds INTEGER NOT NULL DEFAULT 1800,
    join_interval_max_seconds INTEGER NOT NULL DEFAULT 7200,
    next_join_after TIMESTAMP NULL,
    max_messages_per_day INTEGER NOT NULL DEFAULT 30,
    message_interval_seconds INTEGER NOT NULL DEFAULT 300,
    quiet_hours_start VARCHAR(5),
    quiet_hours_end VARCHAR(5),
    keyword_types TEXT,
    risk_level VARCHAR(20) NOT NULL DEFAULT 'normal',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_account_operation_config_account UNIQUE (account_id)
);

CREATE INDEX IF NOT EXISTS idx_account_operation_auto_join ON telegram_account_operation_config (auto_join_enabled);
CREATE INDEX IF NOT EXISTS idx_account_operation_enabled ON telegram_account_operation_config (enabled);

CREATE TABLE IF NOT EXISTS acquisition_auto_join_attempt (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    group_id INTEGER NULL REFERENCES "group"(id) ON DELETE SET NULL,
    telegram_group_id BIGINT,
    group_username VARCHAR(255),
    group_title VARCHAR(500),
    source_keyword VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    reason VARCHAR(255),
    error TEXT,
    attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    joined_at TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS idx_auto_join_account_status ON acquisition_auto_join_attempt (account_id, status);
CREATE INDEX IF NOT EXISTS idx_auto_join_attempted_at ON acquisition_auto_join_attempt (attempted_at);
CREATE INDEX IF NOT EXISTS idx_auto_join_tg_group ON acquisition_auto_join_attempt (telegram_group_id);

CREATE TABLE IF NOT EXISTS ad_creative (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    content TEXT NOT NULL,
    creative_type VARCHAR(30) NOT NULL DEFAULT 'text',
    media_url TEXT,
    link_url TEXT,
    weight INTEGER NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ad_creative_enabled ON ad_creative (enabled);

CREATE TABLE IF NOT EXISTS ad_campaign (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(30) NOT NULL DEFAULT 'draft',
    send_mode VARCHAR(30) NOT NULL DEFAULT 'after_join',
    target_group_levels TEXT,
    start_at TIMESTAMP NULL,
    end_at TIMESTAMP NULL,
    min_wait_after_join_minutes INTEGER NOT NULL DEFAULT 60,
    interval_minutes INTEGER NOT NULL DEFAULT 1440,
    scheduled_times TEXT,
    max_sends_per_group_per_day INTEGER NOT NULL DEFAULT 1,
    max_sends_per_account_per_day INTEGER NOT NULL DEFAULT 20,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ad_campaign_enabled ON ad_campaign (enabled);
CREATE INDEX IF NOT EXISTS idx_ad_campaign_mode ON ad_campaign (send_mode);

CREATE TABLE IF NOT EXISTS account_ad_binding (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    ad_campaign_id INTEGER NOT NULL REFERENCES ad_campaign(id) ON DELETE CASCADE,
    creative_id INTEGER NULL REFERENCES ad_creative(id) ON DELETE SET NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_account_ad_binding UNIQUE (account_id, ad_campaign_id, creative_id)
);

CREATE INDEX IF NOT EXISTS idx_account_ad_binding_account ON account_ad_binding (account_id);
CREATE INDEX IF NOT EXISTS idx_account_ad_binding_campaign ON account_ad_binding (ad_campaign_id);
CREATE INDEX IF NOT EXISTS idx_account_ad_binding_enabled ON account_ad_binding (enabled);

CREATE TABLE IF NOT EXISTS ad_delivery_log (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    group_id INTEGER NULL REFERENCES "group"(id) ON DELETE SET NULL,
    telegram_group_id BIGINT NOT NULL,
    ad_campaign_id INTEGER NOT NULL REFERENCES ad_campaign(id) ON DELETE CASCADE,
    creative_id INTEGER NULL REFERENCES ad_creative(id) ON DELETE SET NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    telegram_message_id BIGINT,
    error TEXT,
    sent_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ad_delivery_account_sent ON ad_delivery_log (account_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_ad_delivery_group_sent ON ad_delivery_log (telegram_group_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_ad_delivery_campaign ON ad_delivery_log (ad_campaign_id);
CREATE INDEX IF NOT EXISTS idx_ad_delivery_status ON ad_delivery_log (status);
