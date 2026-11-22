-- Completed inbox history must not be rescanned for each live peer claim.
-- Production installs these with CREATE INDEX CONCURRENTLY via Alembic 056.
CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_live_peer
ON telegram_event_inbox (account_id, chat_id, id)
WHERE state IN ('pending', 'processing', 'executing', 'reconciliation_required');

CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_live_account
ON telegram_event_inbox (account_id, id)
WHERE state = 'pending';
