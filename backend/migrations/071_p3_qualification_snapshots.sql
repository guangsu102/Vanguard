-- P3 immutable qualification evidence snapshots and local fact versions.
-- Additive migration.  It does not stop or recreate PostgreSQL/Redis.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS qualification_fact_version (
    target_key VARCHAR(500) PRIMARY KEY,
    version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_qualification_fact_version_nonnegative CHECK (version >= 0)
);

CREATE TABLE IF NOT EXISTS qualification_snapshot (
    snapshot_id VARCHAR(128) PRIMARY KEY,
    target_key VARCHAR(500) NOT NULL,
    group_version VARCHAR(128) NOT NULL,
    rule_version VARCHAR(128) NOT NULL,
    evidence_hash VARCHAR(64) NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    verdict VARCHAR(32) NOT NULL,
    source_fact_version VARCHAR(128) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_qualification_snapshot_identity
        UNIQUE (target_key, group_version, rule_version, evidence_hash),
    CONSTRAINT ck_qualification_snapshot_identity
        CHECK (snapshot_id <> '' AND target_key <> '' AND group_version <> ''
               AND rule_version <> '' AND char_length(evidence_hash) = 64
               AND source_fact_version <> ''),
    CONSTRAINT ck_qualification_snapshot_evidence_json
        CHECK (evidence_json <> ''),
    CONSTRAINT ck_qualification_snapshot_verdict
        CHECK (verdict NOT IN ('unknown', 'technical_wait', 'pending'))
);

CREATE INDEX IF NOT EXISTS ix_qualification_snapshot_target_version
    ON qualification_snapshot (target_key, group_version, rule_version);
CREATE INDEX IF NOT EXISTS ix_qualification_snapshot_evidence
    ON qualification_snapshot (evidence_hash);

-- P2 rows created before P3 receive a conservative immutable legacy snapshot
-- so adding the foreign key does not invalidate existing operation history.
INSERT INTO qualification_snapshot (
    snapshot_id, target_key, group_version, rule_version, evidence_hash,
    evidence_json, verdict, source_fact_version, created_at
)
SELECT DISTINCT ON (snapshot_id)
    snapshot_id,
    target_key,
    group_version,
    rule_version,
    evidence_hash,
    '{}',
    'legacy',
    'legacy',
    MIN(created_at) OVER (PARTITION BY snapshot_id)
FROM growth_operation
WHERE snapshot_id IS NOT NULL
  AND snapshot_id <> ''
ON CONFLICT (snapshot_id) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_growth_operation_snapshot'
          AND conrelid = 'growth_operation'::regclass
    ) THEN
        ALTER TABLE growth_operation
            ADD CONSTRAINT fk_growth_operation_snapshot
            FOREIGN KEY (snapshot_id)
            REFERENCES qualification_snapshot(snapshot_id)
            ON DELETE RESTRICT;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION vanguard_guard_qualification_snapshot_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(NEW.snapshot_id, NEW.target_key, NEW.group_version,
           NEW.rule_version, NEW.evidence_hash, NEW.evidence_json,
           NEW.verdict, NEW.source_fact_version, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.snapshot_id, OLD.target_key, OLD.group_version,
           OLD.rule_version, OLD.evidence_hash, OLD.evidence_json,
           OLD.verdict, OLD.source_fact_version, OLD.created_at)
    THEN
        RAISE EXCEPTION 'qualification snapshot is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_qualification_snapshot_identity ON qualification_snapshot;
CREATE TRIGGER trg_qualification_snapshot_identity
BEFORE UPDATE ON qualification_snapshot
FOR EACH ROW EXECUTE FUNCTION vanguard_guard_qualification_snapshot_identity();
