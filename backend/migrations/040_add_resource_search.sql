CREATE TABLE IF NOT EXISTS acquisition_resource_search_run (
    id SERIAL PRIMARY KEY,
    keywords_json TEXT NOT NULL,
    account_ids_json TEXT NOT NULL,
    max_results_per_keyword INTEGER NOT NULL DEFAULT 20,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    total_accounts INTEGER NOT NULL DEFAULT 0,
    completed_accounts INTEGER NOT NULL DEFAULT 0,
    successful_accounts INTEGER NOT NULL DEFAULT 0,
    failed_accounts INTEGER NOT NULL DEFAULT 0,
    raw_result_count INTEGER NOT NULL DEFAULT 0,
    unique_result_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    created_by_id INTEGER,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_resource_search_run_status_created
    ON acquisition_resource_search_run (status, created_at);

CREATE TABLE IF NOT EXISTS acquisition_resource_search_account (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES acquisition_resource_search_run(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES telegram_account(id) ON DELETE SET NULL,
    account_identifier VARCHAR(120) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    keywords_completed INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    flood_wait_seconds INTEGER,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_resource_search_run_account UNIQUE (run_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_search_account_run_status
    ON acquisition_resource_search_account (run_id, status);

CREATE TABLE IF NOT EXISTS acquisition_resource_search_result (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES acquisition_resource_search_run(id) ON DELETE CASCADE,
    dedupe_key VARCHAR(500) NOT NULL,
    telegram_group_id BIGINT,
    title VARCHAR(500) NOT NULL DEFAULT '',
    username VARCHAR(255),
    invite_link VARCHAR(500),
    member_count INTEGER NOT NULL DEFAULT 0,
    is_private BOOLEAN NOT NULL DEFAULT FALSE,
    matched_keywords_json TEXT NOT NULL DEFAULT '[]',
    discovered_by_account_ids_json TEXT NOT NULL DEFAULT '[]',
    discovery_count INTEGER NOT NULL DEFAULT 1,
    review_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    note TEXT,
    first_found_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_found_at TIMESTAMP NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMP,
    reviewed_by_id INTEGER,
    CONSTRAINT uq_resource_search_run_dedupe UNIQUE (run_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_resource_search_result_run_review
    ON acquisition_resource_search_result (run_id, review_status);
CREATE INDEX IF NOT EXISTS idx_resource_search_result_group
    ON acquisition_resource_search_result (telegram_group_id);
CREATE INDEX IF NOT EXISTS idx_resource_search_result_members
    ON acquisition_resource_search_result (member_count);
