-- Add the persisted Telethon session string expected by the current account model.
ALTER TABLE telegram_account
    ADD COLUMN IF NOT EXISTS session_string TEXT;

COMMENT ON COLUMN telegram_account.session_string
    IS 'Telethon session string (用于快速恢复登录)';
