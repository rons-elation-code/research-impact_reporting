-- Migration: 002_attribution_helper
-- Date: 2026-04-22
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
-- Adds: lava_impact.attribution_rank(TEXT) — IMMUTABLE SQL function used
-- by the Spec 0017 upsert_report ON CONFLICT UPDATE logic to prefer
-- stronger attribution tiers.
--
-- Run as master user (postgres) from pgAdmin or psql.

BEGIN;
SET search_path TO lava_impact, public;

CREATE OR REPLACE FUNCTION lava_impact.attribution_rank(attr TEXT)
RETURNS INTEGER
LANGUAGE SQL IMMUTABLE AS $$
  SELECT CASE attr
    WHEN 'own_domain' THEN 3
    WHEN 'platform_verified' THEN 2
    WHEN 'platform_unverified' THEN 1
    ELSE 0
  END
$$;

GRANT EXECUTE ON FUNCTION lava_impact.attribution_rank(TEXT)
  TO app_user1, ro_user1;

INSERT INTO schema_version (version, name)
  VALUES (2, 'attribution_rank_helper')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
