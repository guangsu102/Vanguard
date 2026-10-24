-- P5 release control plane for the canonical SQL migration runner, after P4.
-- Generated from 052_p5_release_control; safe to replay.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS p5_release_record (
    id SERIAL NOT NULL,
    release_id VARCHAR(128) NOT NULL,
    status VARCHAR(16) DEFAULT 'frozen' NOT NULL,
    image_digest VARCHAR(128) DEFAULT '' NOT NULL,
    migration_revision VARCHAR(128) DEFAULT '' NOT NULL,
    mode VARCHAR(16) DEFAULT 'off' NOT NULL,
    cohort_percent INTEGER DEFAULT '0' NOT NULL,
    canary_account_ids_json TEXT DEFAULT '[]' NOT NULL,
    cohort_account_ids_json TEXT DEFAULT '[]' NOT NULL,
    stage_history_json TEXT DEFAULT '[]' NOT NULL,
    enabled_flags_json TEXT DEFAULT '{}' NOT NULL,
    config_snapshot_json TEXT DEFAULT '{}' NOT NULL,
    selected_canary_account_id INTEGER,
    selected_canary_report_json TEXT DEFAULT '{}' NOT NULL,
    baseline_json TEXT DEFAULT '{}' NOT NULL,
    gate_result VARCHAR(8),
    gate_reasons_json TEXT DEFAULT '[]' NOT NULL,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    completed_at TIMESTAMP WITHOUT TIME ZONE,
    rollback_at TIMESTAMP WITHOUT TIME ZONE,
    rollback_reason TEXT,
    rollback_metrics_json TEXT DEFAULT '{}' NOT NULL,
    affected_cohort_json TEXT DEFAULT '[]' NOT NULL,
    recovered_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT p5_release_status CHECK (status IN ('frozen','active','completed','rolled_back')),
    CONSTRAINT p5_release_percent CHECK (cohort_percent >= 0 AND cohort_percent <= 100),
    CONSTRAINT uq_p5_release_record_release_id UNIQUE (release_id)
);

CREATE INDEX IF NOT EXISTS ix_p5_release_record_created ON p5_release_record (created_at);

CREATE TABLE IF NOT EXISTS p5_gate_evaluation (
    id SERIAL NOT NULL,
    release_id VARCHAR(128) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    stage VARCHAR(24) DEFAULT '' NOT NULL,
    result VARCHAR(8) NOT NULL,
    reasons_json TEXT DEFAULT '[]' NOT NULL,
    metrics_json TEXT DEFAULT '{}' NOT NULL,
    cohort_account_ids_json TEXT DEFAULT '[]' NOT NULL,
    window_started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    evaluated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT p5_gate_result CHECK (result IN ('pass','hold','stop'))
);

CREATE INDEX IF NOT EXISTS ix_p5_gate_evaluation_release_time ON p5_gate_evaluation (release_id, evaluated_at);

CREATE INDEX IF NOT EXISTS ix_p5_gate_evaluation_result_time ON p5_gate_evaluation (result, evaluated_at);
