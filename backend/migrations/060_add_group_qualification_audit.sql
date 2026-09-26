CREATE TABLE IF NOT EXISTS group_qualification_audit (
 id SERIAL PRIMARY KEY, batch_id VARCHAR(128) NOT NULL,
 membership_id INTEGER NOT NULL REFERENCES group_account_membership(id),
 account_id INTEGER NOT NULL REFERENCES telegram_account(id),
 group_id INTEGER NOT NULL REFERENCES "group"(id),
 policy_version VARCHAR(64) NOT NULL, content_scope VARCHAR(64) NOT NULL DEFAULT 'text_profile',
 state VARCHAR(32) NOT NULL DEFAULT 'queued', reason VARCHAR(160),
 decision VARCHAR(32) NOT NULL DEFAULT 'unknown', evidence_json TEXT NOT NULL DEFAULT '{}',
 evidence_hash VARCHAR(64), membership_joined_at TIMESTAMP,
 attempts INTEGER NOT NULL DEFAULT 0, next_retry_at TIMESTAMP,
 created_at TIMESTAMP NOT NULL DEFAULT now(), checked_at TIMESTAMP, expires_at TIMESTAMP,
 CONSTRAINT uq_group_qualification_batch_member UNIQUE(batch_id,membership_id)
);
CREATE INDEX IF NOT EXISTS idx_group_qualification_due ON group_qualification_audit(state,next_retry_at);
CREATE INDEX IF NOT EXISTS idx_group_qualification_member ON group_qualification_audit(membership_id,checked_at);
