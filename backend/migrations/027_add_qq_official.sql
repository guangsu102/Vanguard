-- QQ official bot group governance, event monitoring, and command audit.

CREATE TABLE IF NOT EXISTS qq_bot_connection (
    id SERIAL PRIMARY KEY,
    app_id VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(120),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(30) NOT NULL DEFAULT 'offline',
    bot_openid VARCHAR(128),
    session_id VARCHAR(255),
    sequence INTEGER,
    last_heartbeat_at TIMESTAMP,
    last_connected_at TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qq_managed_group (
    id SERIAL PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES qq_bot_connection(id) ON DELETE CASCADE,
    group_openid VARCHAR(128) NOT NULL,
    local_name VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'active',
    monitoring_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    auto_recall_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    receive_all_messages_enabled BOOLEAN,
    proactive_messages_enabled BOOLEAN,
    last_message_at TIMESTAMP,
    bot_added_at TIMESTAMP,
    bot_removed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_qq_group_connection_openid UNIQUE (connection_id, group_openid)
);
CREATE INDEX IF NOT EXISTS idx_qq_managed_group_status ON qq_managed_group(status);
CREATE INDEX IF NOT EXISTS idx_qq_managed_group_last_message ON qq_managed_group(last_message_at);

CREATE TABLE IF NOT EXISTS qq_group_message (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES qq_managed_group(id) ON DELETE CASCADE,
    provider_message_id VARCHAR(255) NOT NULL,
    member_openid VARCHAR(128),
    member_role VARCHAR(30),
    content TEXT,
    attachments_json TEXT,
    is_at_bot BOOLEAN NOT NULL DEFAULT FALSE,
    moderation_status VARCHAR(30) NOT NULL DEFAULT 'unreviewed',
    occurred_at TIMESTAMP NOT NULL,
    recalled_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_qq_message_group_provider UNIQUE (group_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS idx_qq_group_message_time ON qq_group_message(group_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_qq_group_message_member ON qq_group_message(member_openid);

CREATE TABLE IF NOT EXISTS qq_group_event (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES qq_managed_group(id) ON DELETE CASCADE,
    event_id VARCHAR(255) NOT NULL UNIQUE,
    event_type VARCHAR(80) NOT NULL,
    member_openid VARCHAR(128),
    payload_json TEXT,
    occurred_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qq_group_event_group_time ON qq_group_event(group_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_qq_group_event_type ON qq_group_event(event_type);

CREATE TABLE IF NOT EXISTS qq_group_command (
    id VARCHAR(32) PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES qq_managed_group(id) ON DELETE CASCADE,
    command_type VARCHAR(40) NOT NULL,
    payload_json TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    created_by INTEGER,
    provider_message_id VARCHAR(255),
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qq_group_command_status ON qq_group_command(status, created_at);
CREATE INDEX IF NOT EXISTS idx_qq_group_command_group ON qq_group_command(group_id, created_at);
