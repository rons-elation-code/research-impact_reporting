-- ============================================================================
-- Migration 006: Wayback attribution values — bug fix for Spec 0022
-- ============================================================================
--
-- Smoke-test on 2026-04-25 found that Wayback-recovered PDFs failed to
-- write to `reports` because three CHECK constraints reject the new values:
--
--   reports_attr_chk      blocked attribution_confidence = 'wayback_archive'
--   reports_disc_chk      blocked discovered_via         = 'wayback'
--   reports_platform_chk  blocked hosting_platform       = 'wayback'
--
-- Spec 0022 AC13.1 incorrectly stated these columns had no CHECK constraint.
-- This migration extends the three constraints to accept the Wayback values.
-- Existing rows are unaffected.
--
-- HOW TO RUN (pgAdmin):
--   1. Connect as the master user (postgres) on lava_prod1
--   2. Open this file in the Query Tool
--   3. Click Execute (F5)
--   4. Watch the Messages tab for BEFORE / AFTER / DONE notices
--
-- This script is idempotent — running it twice is safe.
-- ============================================================================

SET search_path TO lava_impact, public;

-- ---- BEFORE: show current constraint definitions ----
DO $before$
DECLARE
  v_current_version int;
  v_attr_def        text;
  v_disc_def        text;
  v_platform_def    text;
  v_wayback_attempt_count bigint;
BEGIN
  SELECT COALESCE(MAX(version), 0) INTO v_current_version
    FROM lava_impact.schema_version;

  SELECT pg_get_constraintdef(oid) INTO v_attr_def
    FROM pg_constraint
    WHERE conrelid = 'lava_impact.reports'::regclass
      AND conname  = 'reports_attr_chk';

  SELECT pg_get_constraintdef(oid) INTO v_disc_def
    FROM pg_constraint
    WHERE conrelid = 'lava_impact.reports'::regclass
      AND conname  = 'reports_disc_chk';

  SELECT pg_get_constraintdef(oid) INTO v_platform_def
    FROM pg_constraint
    WHERE conrelid = 'lava_impact.reports'::regclass
      AND conname  = 'reports_platform_chk';

  -- How many Wayback attempts blocked so far?
  SELECT COUNT(*) INTO v_wayback_attempt_count
    FROM lava_impact.fetch_log
    WHERE notes LIKE '%wayback%' OR url_redacted LIKE '%web.archive.org%';

  RAISE NOTICE '------ BEFORE ------';
  RAISE NOTICE 'schema_version max     = %', v_current_version;
  RAISE NOTICE 'reports_attr_chk       = %', v_attr_def;
  RAISE NOTICE 'reports_disc_chk       = %', v_disc_def;
  RAISE NOTICE 'reports_platform_chk   = %', v_platform_def;
  RAISE NOTICE 'wayback fetch_log rows = % (these are blocked at reports table)', v_wayback_attempt_count;
END $before$;

BEGIN;

-- 1) attribution_confidence: add 'wayback_archive'
ALTER TABLE lava_impact.reports DROP CONSTRAINT IF EXISTS reports_attr_chk;
ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_attr_chk
  CHECK (attribution_confidence = ANY (ARRAY[
    'own_domain'::text,
    'platform_verified'::text,
    'platform_unverified'::text,
    'wayback_archive'::text
  ]));

-- 2) discovered_via: add 'wayback'
ALTER TABLE lava_impact.reports DROP CONSTRAINT IF EXISTS reports_disc_chk;
ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_disc_chk
  CHECK (discovered_via = ANY (ARRAY[
    'sitemap'::text,
    'homepage-link'::text,
    'subpage-link'::text,
    'hosting-platform'::text,
    'wayback'::text
  ]));

-- 3) hosting_platform: add 'wayback' (NULL still allowed)
ALTER TABLE lava_impact.reports DROP CONSTRAINT IF EXISTS reports_platform_chk;
ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_platform_chk
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

INSERT INTO lava_impact.schema_version (version, name)
  VALUES (6, 'wayback_attribution_values')
  ON CONFLICT (version) DO NOTHING;

COMMIT;

-- ---- AFTER: verify everything landed ----
DO $after$
DECLARE
  v_current_version int;
  v_attr_ok         boolean;
  v_disc_ok         boolean;
  v_platform_ok     boolean;
BEGIN
  SELECT COALESCE(MAX(version), 0) INTO v_current_version
    FROM lava_impact.schema_version;

  SELECT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lava_impact.reports'::regclass
      AND conname  = 'reports_attr_chk'
      AND pg_get_constraintdef(oid) LIKE '%wayback_archive%'
  ) INTO v_attr_ok;

  SELECT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lava_impact.reports'::regclass
      AND conname  = 'reports_disc_chk'
      AND pg_get_constraintdef(oid) LIKE '%wayback%'
  ) INTO v_disc_ok;

  SELECT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lava_impact.reports'::regclass
      AND conname  = 'reports_platform_chk'
      AND pg_get_constraintdef(oid) LIKE '%wayback%'
  ) INTO v_platform_ok;

  RAISE NOTICE '------ AFTER ------';
  RAISE NOTICE 'schema_version max                       = %', v_current_version;
  RAISE NOTICE 'reports_attr_chk allows wayback_archive? %', v_attr_ok;
  RAISE NOTICE 'reports_disc_chk allows wayback?         %', v_disc_ok;
  RAISE NOTICE 'reports_platform_chk allows wayback?     %', v_platform_ok;

  IF v_current_version >= 6 AND v_attr_ok AND v_disc_ok AND v_platform_ok THEN
    RAISE NOTICE '------ DONE ------ migration 006 applied successfully';
  ELSE
    RAISE WARNING 'migration 006 may not have applied cleanly — review notices above';
  END IF;
END $after$;
