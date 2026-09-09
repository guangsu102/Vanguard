CREATE TABLE IF NOT EXISTS owned_group_assets (
    id SERIAL PRIMARY KEY,
    internal_name VARCHAR(120) NOT NULL,
    telegram_chat_id BIGINT UNIQUE,
    title VARCHAR(255) NOT NULL,
    about TEXT,
    visibility VARCHAR(16) NOT NULL DEFAULT 'public',
    telegram_username VARCHAR(64),
    public_link VARCHAR(255),
    owner_account_id INTEGER NOT NULL
        REFERENCES telegram_account(id) ON DELETE RESTRICT,
    invite_mode VARCHAR(32) NOT NULL DEFAULT 'direct_invite',
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    member_count INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    archived_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_owned_group_assets_status
    ON owned_group_assets (status);

CREATE INDEX IF NOT EXISTS idx_owned_group_assets_owner
    ON owned_group_assets (owner_account_id);

CREATE INDEX IF NOT EXISTS idx_owned_group_assets_visibility
    ON owned_group_assets (visibility);

CREATE TABLE IF NOT EXISTS owned_group_operations (
    id SERIAL PRIMARY KEY,
    group_asset_id INTEGER NOT NULL
        REFERENCES owned_group_assets(id) ON DELETE CASCADE,
    operation_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    selection_snapshot TEXT NOT NULL,
    selection_snapshot_hash VARCHAR(64) NOT NULL,
    config_snapshot TEXT NOT NULL,
    config_snapshot_hash VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    planned_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    schedule_at TIMESTAMP WITHOUT TIME ZONE,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    last_error TEXT,
    created_by INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_owned_group_operations_group_status
    ON owned_group_operations (group_asset_id, status);

CREATE INDEX IF NOT EXISTS idx_owned_group_operations_schedule
    ON owned_group_operations (status, schedule_at);

CREATE TABLE IF NOT EXISTS owned_group_operation_items (
    id SERIAL PRIMARY KEY,
    operation_id INTEGER NOT NULL
        REFERENCES owned_group_operations(id) ON DELETE CASCADE,
    resource_type VARCHAR(16) NOT NULL,
    resource_id INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason_code VARCHAR(64),
    error_message TEXT,
    next_retry_at TIMESTAMP WITHOUT TIME ZONE,
    invited_at TIMESTAMP WITHOUT TIME ZONE,
    joined_at TIMESTAMP WITHOUT TIME ZONE,
    verified_at TIMESTAMP WITHOUT TIME ZONE,
    admin_required BOOLEAN NOT NULL DEFAULT FALSE,
    admin_permissions TEXT,
    admin_title VARCHAR(64),
    lease_id VARCHAR(128),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_owned_group_operation_resource
        UNIQUE (operation_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_owned_group_items_status_retry
    ON owned_group_operation_items (status, next_retry_at);

CREATE INDEX IF NOT EXISTS idx_owned_group_items_resource
    ON owned_group_operation_items (resource_type, resource_id);

CREATE TABLE IF NOT EXISTS owned_group_memberships (
    id SERIAL PRIMARY KEY,
    group_asset_id INTEGER NOT NULL
        REFERENCES owned_group_assets(id) ON DELETE CASCADE,
    resource_type VARCHAR(16) NOT NULL,
    resource_id INTEGER NOT NULL,
    telegram_user_id BIGINT,
    status VARCHAR(32) NOT NULL DEFAULT 'member_verified',
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    admin_title VARCHAR(64),
    permissions_snapshot TEXT,
    joined_at TIMESTAMP WITHOUT TIME ZONE,
    last_verified_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_owned_group_membership_resource
        UNIQUE (group_asset_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_owned_group_memberships_resource
    ON owned_group_memberships (resource_type, resource_id);

CREATE INDEX IF NOT EXISTS idx_owned_group_memberships_status
    ON owned_group_memberships (status);

CREATE TABLE IF NOT EXISTS owned_group_admin_assignments (
    id SERIAL PRIMARY KEY,
    group_asset_id INTEGER NOT NULL
        REFERENCES owned_group_assets(id) ON DELETE CASCADE,
    resource_type VARCHAR(16) NOT NULL,
    resource_id INTEGER NOT NULL,
    permissions_snapshot TEXT NOT NULL DEFAULT '{}',
    admin_title VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    verified_at TIMESTAMP WITHOUT TIME ZONE,
    created_by INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_owned_group_admin_resource
        UNIQUE (group_asset_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_owned_group_admin_status
    ON owned_group_admin_assignments (status);

CREATE TABLE IF NOT EXISTS owned_group_invite_links (
    id SERIAL PRIMARY KEY,
    group_asset_id INTEGER NOT NULL
        REFERENCES owned_group_assets(id) ON DELETE CASCADE,
    link_type VARCHAR(32) NOT NULL,
    link_ciphertext TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_owned_group_invite_links_active
    ON owned_group_invite_links (group_asset_id, is_active);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owned_group_invite_links_one_active
    ON owned_group_invite_links (group_asset_id)
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS owned_bot_profiles (
    id SERIAL PRIMARY KEY,
    owner_account_id INTEGER NOT NULL
        REFERENCES telegram_account(id) ON DELETE RESTRICT,
    account_id INTEGER NOT NULL UNIQUE
        REFERENCES telegram_account(id) ON DELETE CASCADE,
    bot_user_id BIGINT,
    bot_username VARCHAR(120),
    display_name VARCHAR(120),
    token_ciphertext TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending_verification',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_verified_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_owned_bot_profiles_owner
    ON owned_bot_profiles (owner_account_id);

CREATE INDEX IF NOT EXISTS idx_owned_bot_profiles_status
    ON owned_bot_profiles (status);

CREATE TABLE IF NOT EXISTS owned_group_audit_events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    group_asset_id INTEGER
        REFERENCES owned_group_assets(id) ON DELETE SET NULL,
    operation_id INTEGER
        REFERENCES owned_group_operations(id) ON DELETE SET NULL,
    operation_item_id INTEGER
        REFERENCES owned_group_operation_items(id) ON DELETE SET NULL,
    resource_type VARCHAR(16),
    resource_id INTEGER,
    actor_id INTEGER,
    before_state TEXT,
    after_state TEXT,
    result VARCHAR(32) NOT NULL DEFAULT 'success',
    reason_code VARCHAR(64),
    correlation_id VARCHAR(128),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_owned_group_audit_group_time
    ON owned_group_audit_events (group_asset_id, created_at);

CREATE INDEX IF NOT EXISTS idx_owned_group_audit_operation_time
    ON owned_group_audit_events (operation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_owned_group_audit_resource_time
    ON owned_group_audit_events (resource_type, resource_id, created_at);
