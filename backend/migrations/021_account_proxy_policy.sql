-- Account-level proxy policy for dynamic/static/no-proxy Telegram sessions.

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS proxy_mode VARCHAR(20) NOT NULL DEFAULT 'dynamic';

ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS static_proxy_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_telegram_account_static_proxy_id_proxy'
    ) THEN
        ALTER TABLE telegram_account
            ADD CONSTRAINT fk_telegram_account_static_proxy_id_proxy
            FOREIGN KEY (static_proxy_id)
            REFERENCES proxy(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_account_static_proxy
    ON telegram_account(static_proxy_id);

CREATE INDEX IF NOT EXISTS idx_account_proxy_mode
    ON telegram_account(proxy_mode);

UPDATE telegram_account
SET proxy_mode = 'dynamic'
WHERE proxy_mode IS NULL OR proxy_mode = '';

COMMENT ON COLUMN telegram_account.proxy_mode
    IS 'Proxy policy: dynamic/static/none';

COMMENT ON COLUMN telegram_account.static_proxy_id
    IS 'Static bound proxy id';
