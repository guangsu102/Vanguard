-- Add review workflow for AI-generated marketing trigger keywords.

ALTER TABLE acquisition_keyword_trigger
    ADD COLUMN IF NOT EXISTS requires_review BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_trigger_requires_review
    ON acquisition_keyword_trigger(requires_review);
