-- Migration 001: Add workload metadata to existing GreenScale jobs
-- Existing jobs are preserved; new columns allow NULL values.

BEGIN;

ALTER TABLE public.jobs
    ADD COLUMN IF NOT EXISTS workload_type VARCHAR(40),
    ADD COLUMN IF NOT EXISTS input_size_mb INTEGER;

COMMIT;