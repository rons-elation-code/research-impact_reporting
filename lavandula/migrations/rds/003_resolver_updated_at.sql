-- Migration: 003_resolver_updated_at
-- Date: 2026-04-23
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
-- Adds: resolver_updated_at TIMESTAMPTZ column to nonprofits_seed
--
-- Run as master user (postgres) from pgAdmin or psql.

BEGIN;
SET search_path TO lava_impact, public;

ALTER TABLE nonprofits_seed
  ADD COLUMN IF NOT EXISTS resolver_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_seed_resolver_updated
  ON nonprofits_seed(resolver_updated_at);

INSERT INTO schema_version (version, name)
  VALUES (3, 'resolver_updated_at')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
