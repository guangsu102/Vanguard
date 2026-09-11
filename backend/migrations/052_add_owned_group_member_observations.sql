-- Stage 5: Guardian-observed membership facts for self-owned groups.
--
-- This table intentionally starts empty. It is not a Telegram roster cache and
-- no historical User, Verification, Violation or message data is backfilled.

CREATE TABLE IF NOT EXISTS owned_group_member_observations (
    id SERIAL PRIMARY KEY,
    group_asset_id INTEGER NOT NULL
        REFERENCES owned_group_assets(id) ON DELETE CASCADE,
    telegram_user_id BIGINT NOT NULL,
    is_bot BOOLEAN NOT NULL,
    username_snapshot VARCHAR(120),
    display_name_snapshot VARCHAR(200),
    presence_status VARCHAR(16) NOT NULL DEFAULT 'present',
    last_event_type VARCHAR(32) NOT NULL,
    source_bot_account_id INTEGER
        REFERENCES telegram_account(id) ON DELETE SET NULL,
    last_update_id BIGINT NOT NULL,
    first_observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    joined_at TIMESTAMP WITH TIME ZONE,
    left_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_owned_group_member_observations_asset_user
        UNIQUE (group_asset_id, telegram_user_id),
    CONSTRAINT ck_owned_group_member_observations_presence_status
        CHECK (presence_status IN ('present', 'left')),
    CONSTRAINT ck_owned_group_member_observations_last_event_type
        CHECK (last_event_type IN ('message', 'new_chat_member', 'left_chat_member'))
);

CREATE INDEX IF NOT EXISTS idx_owned_group_member_observations_presence
    ON owned_group_member_observations
        (group_asset_id, presence_status, last_observed_at);
CREATE INDEX IF NOT EXISTS idx_owned_group_member_observations_last_observed
    ON owned_group_member_observations (group_asset_id, last_observed_at);
CREATE INDEX IF NOT EXISTS idx_owned_group_member_observations_telegram_user
    ON owned_group_member_observations (telegram_user_id);
CREATE INDEX IF NOT EXISTS idx_owned_group_member_observations_source_bot
    ON owned_group_member_observations (source_bot_account_id);

COMMENT ON TABLE owned_group_member_observations
    IS 'Partial Guardian-observed membership facts; never a complete Telegram roster';
COMMENT ON COLUMN owned_group_member_observations.last_update_id
    IS 'Telegram Bot API update_id in the source Guardian bot sequence namespace';
