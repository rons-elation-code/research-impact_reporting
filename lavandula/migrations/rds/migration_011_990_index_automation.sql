-- Migration 011: 990 Filing Index Automation & S3 Archive (Spec 0030)
--
-- Adds columns to filing_index for automation tracking, creates
-- index_refresh_log and filing_status_audit tables.

BEGIN;

-- Allow NULL xml_batch_id for 2017-2023 filings (already nullable from 010
-- CREATE TABLE, but ensure it's explicit)
ALTER TABLE lava_corpus.filing_index
  ALTER COLUMN xml_batch_id DROP NOT NULL;

-- New columns for automation.
-- first_indexed_at/last_seen_at added WITHOUT defaults so existing rows get NULL.
-- The bulk loader sets first_indexed_at = now() on INSERT and last_seen_at = now()
-- on both INSERT and UPDATE.
ALTER TABLE lava_corpus.filing_index
  ADD COLUMN IF NOT EXISTS first_indexed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS s3_xml_key TEXT,
  ADD COLUMN IF NOT EXISTS zip_checksum TEXT;

-- Index for incremental processing window queries
CREATE INDEX IF NOT EXISTS idx_filing_first_indexed
  ON lava_corpus.filing_index(first_indexed_at)
  WHERE first_indexed_at IS NOT NULL;

-- Index for auto-process worker joins
CREATE INDEX IF NOT EXISTS idx_filing_status_batch
  ON lava_corpus.filing_index(status, xml_batch_id)
  WHERE status = 'indexed' AND xml_batch_id IS NOT NULL;

-- Refresh log: one row per year per loader run
CREATE TABLE IF NOT EXISTS lava_corpus.index_refresh_log (
    id              SERIAL PRIMARY KEY,
    filing_year     INTEGER NOT NULL,
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_scanned    INTEGER NOT NULL DEFAULT 0,
    rows_inserted   INTEGER NOT NULL DEFAULT 0,
    rows_skipped    INTEGER NOT NULL DEFAULT 0,
    duration_sec    NUMERIC(8,2)
);

-- Audit log for status resets
CREATE TABLE IF NOT EXISTS lava_corpus.filing_status_audit (
    id              SERIAL PRIMARY KEY,
    filing_id       TEXT NOT NULL,
    old_status      TEXT NOT NULL,
    new_status      TEXT NOT NULL,
    reset_by        TEXT NOT NULL,
    reset_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    command_args    TEXT
);

-- GRANTs
GRANT SELECT, INSERT ON lava_corpus.index_refresh_log TO app_user1;
GRANT SELECT ON lava_corpus.index_refresh_log TO ro_user1;
GRANT USAGE, SELECT ON SEQUENCE lava_corpus.index_refresh_log_id_seq TO app_user1;

GRANT SELECT, INSERT ON lava_corpus.filing_status_audit TO app_user1;
GRANT SELECT ON lava_corpus.filing_status_audit TO ro_user1;
GRANT USAGE, SELECT ON SEQUENCE lava_corpus.filing_status_audit_id_seq TO app_user1;

COMMIT;
