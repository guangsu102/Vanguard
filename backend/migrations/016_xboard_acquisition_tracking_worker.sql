-- XBoard tracking convergence and Telegram worker status foundation

ALTER TABLE acquisition_tracking
    ADD COLUMN IF NOT EXISTS coupon_status VARCHAR(30),
    ADD COLUMN IF NOT EXISTS coupon_code VARCHAR(100),
    ADD COLUMN IF NOT EXISTS trial_granted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS external_user_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_acquisition_tracking_external_user_id
    ON acquisition_tracking(external_user_id);

CREATE TABLE IF NOT EXISTS telegram_worker_status (
    id SERIAL PRIMARY KEY,
    worker_id VARCHAR(120) NOT NULL UNIQUE,
    role VARCHAR(50) NOT NULL,
    account_id INTEGER REFERENCES telegram_account(id) ON DELETE SET NULL,
    bot_profile_id INTEGER REFERENCES guardian_bot_profile(id) ON DELETE SET NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'starting',
    last_heartbeat_at TIMESTAMP,
    last_error TEXT,
    metadata_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telegram_worker_status_role
    ON telegram_worker_status(role);

CREATE INDEX IF NOT EXISTS idx_telegram_worker_status_status
    ON telegram_worker_status(status);
