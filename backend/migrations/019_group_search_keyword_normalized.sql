-- Add normalized signatures for scalable group-search keyword deduplication.

ALTER TABLE acquisition_group_search_keyword
    ADD COLUMN IF NOT EXISTS normalized_text VARCHAR(255) NOT NULL DEFAULT '';

UPDATE acquisition_group_search_keyword
SET normalized_text = LOWER(REGEXP_REPLACE(BTRIM(text), '\s+', ' ', 'g'))
WHERE normalized_text IS NULL OR normalized_text = '';

WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY keyword_type, normalized_text
            ORDER BY
                enabled DESC,
                CASE status
                    WHEN 'APPROVED' THEN 0
                    WHEN 'PENDING' THEN 1
                    ELSE 2
                END,
                id ASC
        ) AS duplicate_rank
    FROM acquisition_group_search_keyword
    WHERE normalized_text <> ''
)
UPDATE acquisition_group_search_keyword keyword
SET
    status = 'DISCARDED',
    enabled = FALSE,
    requires_review = FALSE,
    updated_at = NOW()
FROM ranked
WHERE keyword.id = ranked.id
  AND ranked.duplicate_rank > 1
  AND keyword.status <> 'DISCARDED';

CREATE INDEX IF NOT EXISTS idx_group_search_keyword_normalized
    ON acquisition_group_search_keyword(keyword_type, normalized_text);

CREATE INDEX IF NOT EXISTS idx_group_search_keyword_list
    ON acquisition_group_search_keyword(updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_group_search_keyword_searchable
    ON acquisition_group_search_keyword(keyword_type, status, enabled, used_at, created_at DESC, id DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_search_keyword_normalized_active
    ON acquisition_group_search_keyword(keyword_type, normalized_text)
    WHERE normalized_text <> '' AND status <> 'DISCARDED';
