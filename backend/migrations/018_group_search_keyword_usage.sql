-- Track one-time use of group-search keywords for auto-join scans.

ALTER TABLE acquisition_group_search_keyword
    ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE acquisition_group_search_keyword
    ADD COLUMN IF NOT EXISTS used_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_group_search_keyword_used
    ON acquisition_group_search_keyword(used_at);

UPDATE acquisition_group_search_keyword keyword
SET
    used_at = COALESCE(keyword.used_at, NOW()),
    use_count = GREATEST(keyword.use_count, 1),
    trigger_count = GREATEST(keyword.trigger_count, 1),
    updated_at = NOW()
WHERE keyword.used_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM acquisition_auto_join_attempt attempt
      WHERE attempt.source_keyword IS NOT NULL
        AND LOWER(TRIM(attempt.source_keyword)) = LOWER(TRIM(keyword.text))
  );

UPDATE acquisition_group_search_keyword keyword
SET
    used_at = COALESCE(keyword.used_at, NOW()),
    use_count = GREATEST(keyword.use_count, 1),
    trigger_count = GREATEST(keyword.trigger_count, 1),
    updated_at = NOW()
WHERE keyword.used_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM acquisition_group_search search
      WHERE LOWER(TRIM(search.keyword)) = LOWER(TRIM(keyword.text))
  );
