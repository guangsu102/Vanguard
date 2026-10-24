-- Executed atomically with checksum/history by apply_sql_migrations.py.
-- Additive only: never backfill or rewrite historical business records.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS account_budget_reservation (
    reservation_id VARCHAR(160) PRIMARY KEY,
    account_id INTEGER NOT NULL,
    workload VARCHAR(32) NOT NULL,
    lane VARCHAR(16) NOT NULL,
    window_start TIMESTAMP NOT NULL,
    reserved_reads INTEGER NOT NULL,
    consumed_reads INTEGER NOT NULL DEFAULT 0,
    state VARCHAR(16) NOT NULL DEFAULT 'reserved',
    lease_expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    settled_at TIMESTAMP,
    reason VARCHAR(100),
    CONSTRAINT ck_account_budget_reservation_budget_reads CHECK (reserved_reads >= 0 AND consumed_reads >= 0 AND consumed_reads <= reserved_reads),
    CONSTRAINT ck_account_budget_reservation_budget_state CHECK (state IN ('reserved','settled','released','expired')),
    CONSTRAINT ck_account_budget_reservation_budget_lane CHECK (lane IN ('ad','survival','critical','sync','routine','listener'))
);
CREATE INDEX IF NOT EXISTS ix_budget_reservation_expiry ON account_budget_reservation (state, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_budget_reservation_account ON account_budget_reservation (account_id, created_at);

CREATE TABLE IF NOT EXISTS account_budget_snapshot (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    "window" VARCHAR(8) NOT NULL,
    window_start TIMESTAMP NOT NULL,
    lane VARCHAR(16) NOT NULL,
    "limit" INTEGER NOT NULL,
    used INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    read_factor DOUBLE PRECISION NOT NULL,
    loan_json TEXT NOT NULL DEFAULT '{}',
    snapshot_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_budget_snapshot_tick UNIQUE (account_id, "window", lane, snapshot_at),
    CONSTRAINT ck_account_budget_snapshot_snapshot_reads CHECK ("limit" >= 0 AND used >= 0 AND remaining >= 0)
);
CREATE INDEX IF NOT EXISTS ix_budget_snapshot_time ON account_budget_snapshot (snapshot_at);

CREATE TABLE IF NOT EXISTS reconciliation_task (
    id SERIAL PRIMARY KEY,
    task_key VARCHAR(120) NOT NULL UNIQUE,
    kind VARCHAR(12) NOT NULL,
    source_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    state VARCHAR(24) NOT NULL DEFAULT 'pending',
    available_at TIMESTAMP NOT NULL DEFAULT now(),
    lease_owner VARCHAR(120),
    lease_expires_at TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    finished_at TIMESTAMP,
    published_at TIMESTAMP,
    CONSTRAINT ck_reconciliation_task_reconciliation_kind CHECK (kind IN ('join','ad','inbox')),
    CONSTRAINT ck_reconciliation_task_reconciliation_state CHECK (state IN ('pending','leased','succeeded','manual_required','cancelled')),
    CONSTRAINT ck_reconciliation_task_reconciliation_attempts CHECK (attempts >= 0)
);
CREATE INDEX IF NOT EXISTS ix_reconciliation_ready ON reconciliation_task (state, available_at, priority, id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_lease ON reconciliation_task (state, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_account ON reconciliation_task (account_id, kind, source_id);
