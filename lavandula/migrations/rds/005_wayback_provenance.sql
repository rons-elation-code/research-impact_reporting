-- Migration: 005_wayback_provenance
-- Date: 2026-04-25
-- Spec: 0022 (Wayback Machine CDX Fallback)
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
--
-- Adds:
--   reports.original_source_url_redacted TEXT NULL  (AC13)
--   crawled_orgs.notes TEXT NULL                   (AC14, two-strikes empty rule)
--
-- attribution_confidence is currently free-text (no CHECK constraint),
-- so adding 'wayback_archive' value requires no schema change.

BEGIN;
SET search_path TO lava_impact, public;

ALTER TABLE reports
  ADD COLUMN IF NOT EXISTS original_source_url_redacted TEXT NULL;

ALTER TABLE crawled_orgs
  ADD COLUMN IF NOT EXISTS notes TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_reports_discovered_via
  ON reports(discovered_via);

INSERT INTO schema_version (version, name)
  VALUES (5, 'wayback_provenance')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
