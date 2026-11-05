-- Migration 003: Preserve the structured JSON returned by the job LLM.
BEGIN;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS llm_response TEXT;

COMMIT;
