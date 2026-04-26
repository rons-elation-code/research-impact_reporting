-- Migration: 006_wayback_attribution_values
-- Date: 2026-04-25
-- Spec: 0022 (Wayback Machine CDX Fallback) — bug-fix follow-up
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
--
-- Spec 0022 AC13.1 incorrectly claimed `attribution_confidence` had no
-- CHECK constraint. It does — and so do `discovered_via` and
-- `hosting_platform`. The Wayback fallback's required values
-- ('wayback_archive', 'wayback', 'wayback') were rejected at write time,
-- blocking all Wayback-recovered PDFs from landing in `reports`.
--
-- This migration extends the three CHECK constraints to accept the new values.
-- Existing rows are unaffected (defaults still in the allowed set).
--
-- Run as master user (postgres) from pgAdmin or psql.

BEGIN;
SET search_path TO lava_impact, public;

-- 1) attribution_confidence: add 'wayback_archive'
ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_attr_chk;
ALTER TABLE reports ADD CONSTRAINT reports_attr_chk
  CHECK (attribution_confidence = ANY (ARRAY[
    'own_domain'::text,
    'platform_verified'::text,
    'platform_unverified'::text,
    'wayback_archive'::text
  ]));

-- 2) discovered_via: add 'wayback'
ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_disc_chk;
ALTER TABLE reports ADD CONSTRAINT reports_disc_chk
  CHECK (discovered_via = ANY (ARRAY[
    'sitemap'::text,
    'homepage-link'::text,
    'subpage-link'::text,
    'hosting-platform'::text,
    'wayback'::text
  ]));

-- 3) hosting_platform: add 'wayback' (NULL still allowed)
ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_platform_chk;
ALTER TABLE reports ADD CONSTRAINT reports_platform_chk
  CHECK (
    hosting_platform IS NULL
    OR hosting_platform = ANY (ARRAY[
      'issuu'::text,
      'flipsnack'::text,
      'canva'::text,
      'own-domain'::text,
      'own-cms'::text,
      'wayback'::text
    ])
  );

INSERT INTO schema_version (version, name)
  VALUES (6, 'wayback_attribution_values')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
