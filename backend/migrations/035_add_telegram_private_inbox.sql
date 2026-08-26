DO $$
BEGIN
    IF to_regclass('public.alembic_version') IS NOT NULL THEN
        ALTER TABLE alembic_version
            ALTER COLUMN version_num TYPE VARCHAR(128);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS telegram_private_conversation (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    peer_telegram_id BIGINT NOT NULL,
    peer_username VARCHAR(100),
    peer_display_name VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    handling_mode VARCHAR(20) NOT NULL DEFAULT 'auto',
    assigned_admin_id INTEGER,
    unread_count INTEGER NOT NULL DEFAULT 0,
    last_message_preview VARCHAR(255),
    last_message_direction VARCHAR(20),
    last_message_at TIMESTAMP WITHOUT TIME ZONE,
    last_inbound_at TIMESTAMP WITHOUT TIME ZONE,
    last_outbound_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_private_conversation_account_peer
        UNIQUE (account_id, peer_telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_private_conversation_last_message
    ON telegram_private_conversation (status, last_message_at);

CREATE INDEX IF NOT EXISTS idx_private_conversation_account_last
    ON telegram_private_conversation (account_id, last_message_at);

CREATE INDEX IF NOT EXISTS idx_private_conversation_unread
    ON telegram_private_conversation (unread_count, last_message_at);

CREATE TABLE IF NOT EXISTS telegram_private_message (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES telegram_private_conversation(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    peer_telegram_id BIGINT NOT NULL,
    telegram_message_id BIGINT,
    reply_to_telegram_message_id BIGINT,
    direction VARCHAR(20) NOT NULL,
    source VARCHAR(20) NOT NULL,
    message_type VARCHAR(30) NOT NULL DEFAULT 'text',
    content TEXT,
    media_json TEXT,
    status VARCHAR(20) NOT NULL,
    operator_id INTEGER,
    client_request_id VARCHAR(64),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    error_message TEXT,
    occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    sent_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_private_message_account_peer_telegram_id
        UNIQUE (account_id, peer_telegram_id, telegram_message_id),
    CONSTRAINT uq_private_message_client_request
        UNIQUE (client_request_id)
);

CREATE INDEX IF NOT EXISTS idx_private_message_conversation_time
    ON telegram_private_message (conversation_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_private_message_outbox
    ON telegram_private_message (direction, status, next_attempt_at);
