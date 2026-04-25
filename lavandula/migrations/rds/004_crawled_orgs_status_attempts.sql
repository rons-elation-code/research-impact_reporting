-- Migration: 004_crawled_orgs_status_attempts
-- Date: 2026-04-25
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
-- Adds: status + attempts columns to crawled_orgs for retry-with-cap semantics.
--
-- Spec 0021 transient handling now writes a row (status='transient') so retries
-- can be tracked. After config.MAX_TRANSIENT_ATTEMPTS (default 3) failed runs,
-- the row is auto-promoted to status='permanent_skip' by the upsert SQL. This
-- removes the previous "retry forever on every resume" behavior while keeping
-- denial-of-crawl protections in place for the first N attempts.
--
-- Run as master user (postgres) from pgAdmin or psql.

BEGIN;
SET search_path TO lava_impact, public;

ALTER TABLE crawled_orgs
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ok',
  ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 1;

-- Existing rows are all from successful crawls; defaults are correct.

CREATE INDEX IF NOT EXISTS idx_crawled_orgs_status
  ON crawled_orgs(status);

INSERT INTO schema_version (version, name)
  VALUES (4, 'crawled_orgs_status_attempts')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
