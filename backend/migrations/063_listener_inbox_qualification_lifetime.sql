CREATE TABLE IF NOT EXISTS telegram_event_inbox (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
    event_key VARCHAR(64) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    chat_id BIGINT,
    payload_json TEXT NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    external_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    last_error VARCHAR(100),
    CONSTRAINT uq_telegram_event_inbox_identity UNIQUE(account_id, event_key)
);
CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_pending ON telegram_event_inbox(state, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS ix_telegram_event_inbox_peer ON telegram_event_inbox(account_id, chat_id, id);

-- Only migrate the latest previously approved evidence with the exact old TTL.
-- Explicit invalidations, rejected decisions, old memberships and manual pauses stay blocked.
UPDATE group_qualification_audit AS q
SET expires_at = q.expires_at + INTERVAL '696 hours',
    next_retry_at = GREATEST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', q.expires_at + INTERVAL '672 hours')
FROM group_account_membership AS m
WHERE q.membership_id = m.id
  AND q.account_id = m.account_id AND q.group_id = m.group_id
  AND q.membership_joined_at = m.joined_at
  AND m.status = 'joined' AND m.left_at IS NULL
  AND m.review_status = 'approved' AND m.ad_status = 'active'
  AND q.policy_version = 'pp-ai-qualification-v3' AND q.content_scope = 'text_profile'
  AND q.state = 'completed' AND q.decision IN ('allowed','trial')
  AND q.checked_at IS NOT NULL
  AND q.expires_at = LEAST(q.checked_at, COALESCE(NULLIF(q.evidence_json::jsonb->>'collected_at','')::timestamp, q.checked_at)) + INTERVAL '24 hours'
  AND q.expires_at + INTERVAL '696 hours' > CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
  AND NOT EXISTS (SELECT 1 FROM group_qualification_audit n WHERE n.membership_id=q.membership_id AND n.id>q.id AND n.state<>'cancelled');

UPDATE group_account_membership AS m
SET review_next_at = q.next_retry_at
FROM group_qualification_audit AS q
WHERE q.membership_id = m.id AND m.status='joined' AND m.review_status='approved'
  AND q.state='completed' AND q.decision IN ('allowed','trial')
  AND q.expires_at > CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
  AND NOT EXISTS (SELECT 1 FROM group_qualification_audit n WHERE n.membership_id=q.membership_id AND n.id>q.id AND n.state<>'cancelled');
