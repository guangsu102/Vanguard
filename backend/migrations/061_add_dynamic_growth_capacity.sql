ALTER TABLE telegram_account ADD COLUMN IF NOT EXISTS age_attestation_json TEXT;
ALTER TABLE telegram_account_operation_config ADD COLUMN IF NOT EXISTS dynamic_capacity_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE telegram_account_operation_config ADD COLUMN IF NOT EXISTS max_ads_per_day INTEGER NOT NULL DEFAULT 30;
ALTER TABLE telegram_account_operation_config ADD COLUMN IF NOT EXISTS max_verification_messages_per_day INTEGER NOT NULL DEFAULT 30;
ALTER TABLE telegram_account_operation_config ADD COLUMN IF NOT EXISTS max_diagnostic_messages_per_day INTEGER NOT NULL DEFAULT 2;
ALTER TABLE acquisition_auto_join_attempt ADD COLUMN IF NOT EXISTS reconciliation_status VARCHAR(24) NOT NULL DEFAULT 'pending';
ALTER TABLE acquisition_auto_join_attempt ADD COLUMN IF NOT EXISTS reconciliation_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ad_delivery_log ADD COLUMN IF NOT EXISTS qualification_context_json TEXT;
ALTER TABLE ad_delivery_log ADD COLUMN IF NOT EXISTS survival_claim_token VARCHAR(64);
ALTER TABLE ad_delivery_log ADD COLUMN IF NOT EXISTS survival_claim_expires_at TIMESTAMP;
ALTER TABLE ad_delivery_log ADD COLUMN IF NOT EXISTS survival_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS account_outbound_attempt (
 id SERIAL PRIMARY KEY,
 account_id INTEGER NOT NULL REFERENCES telegram_account(id) ON DELETE CASCADE,
 attempt_key VARCHAR(128) NOT NULL UNIQUE,
 category VARCHAR(24) NOT NULL CHECK (category IN ('ad','verification','diagnostic','other')),
 target_key VARCHAR(128) NOT NULL,
 state VARCHAR(24) NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','attempted','succeeded','failed','unknown','cancelled')),
 created_at TIMESTAMP NOT NULL DEFAULT now(), lease_expires_at TIMESTAMP,
 attempted_at TIMESTAMP, completed_at TIMESTAMP, message_id BIGINT,
 error_code VARCHAR(160), context_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbound_account_category_time ON account_outbound_attempt(account_id,category,attempted_at);
CREATE INDEX IF NOT EXISTS idx_outbound_target_state ON account_outbound_attempt(target_key,state);
CREATE INDEX IF NOT EXISTS idx_outbound_lease ON account_outbound_attempt(state,lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_auto_join_reconciliation_state_due ON acquisition_auto_join_attempt(reconciliation_status,reconciliation_next_at);
CREATE INDEX IF NOT EXISTS idx_ad_survival_claim ON ad_delivery_log(survival_claim_expires_at,survival_check_due_at);
