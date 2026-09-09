ALTER TABLE acquisition_resource_search_run
    ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR(64);

ALTER TABLE acquisition_resource_search_run
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP;

ALTER TABLE acquisition_resource_search_run
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP;

ALTER TABLE acquisition_resource_search_account
    ADD COLUMN IF NOT EXISTS successful_keywords INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_resource_search_run_task
    ON acquisition_resource_search_run (celery_task_id);

CREATE INDEX IF NOT EXISTS idx_resource_search_run_heartbeat
    ON acquisition_resource_search_run (status, heartbeat_at);
