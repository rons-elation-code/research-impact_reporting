-- 008_preflight.sql
-- Run ALL queries in PGAdmin BEFORE executing 008_rename_reports_to_corpus.sql.
-- If any check fails, do NOT proceed. Investigate the discrepancy first.

-- 1. Verify table exists
SELECT tablename FROM pg_tables
WHERE schemaname = 'lava_impact' AND tablename = 'reports';
-- Expected: 1 row

-- 2. Verify all 20 constraints exist
SELECT conname FROM pg_constraint
WHERE conrelid = 'lava_impact.reports'::regclass
ORDER BY conname;
-- Expected (20 rows):
--   reports_attr_chk, reports_class_chk, reports_conf_chk,
--   reports_creator_chk, reports_ct_chk, reports_disc_chk,
--   reports_embed_chk, reports_et_chk, reports_fpt_len_chk,
--   reports_js_chk, reports_launch_chk, reports_mg_chk,
--   reports_mt_chk, reports_platform_chk, reports_producer_chk,
--   reports_redirect_chk, reports_sha_len_chk, reports_size_chk,
--   reports_uri_chk, reports_year_src_chk
-- If any constraint is MISSING → do not proceed; investigate.

-- 3. Verify all 8 indexes exist
SELECT indexname FROM pg_indexes
WHERE schemaname = 'lava_impact' AND tablename = 'reports'
  AND indexname LIKE 'idx_reports_%'
ORDER BY indexname;
-- Expected (8 rows):
--   idx_reports_classification, idx_reports_discovered_via,
--   idx_reports_ein, idx_reports_event_type,
--   idx_reports_material_group, idx_reports_material_type,
--   idx_reports_platform, idx_reports_year

-- 4. Verify view exists
SELECT viewname FROM pg_views
WHERE schemaname = 'lava_impact' AND viewname = 'reports_public';
-- Expected: 1 row

-- 5. Verify no dependent views on reports_public
SELECT dependent.relname
FROM pg_depend d
JOIN pg_rewrite r ON d.objid = r.oid
JOIN pg_class dependent ON r.ev_class = dependent.oid
JOIN pg_class source ON d.refobjid = source.oid
WHERE source.relname = 'reports_public'
  AND dependent.relname != 'reports_public';
-- Expected: 0 rows

-- 6. Verify no sequences owned by the table
SELECT c.relname AS sequence_name
FROM pg_class c
JOIN pg_depend d ON d.objid = c.oid
JOIN pg_class t ON t.oid = d.refobjid
WHERE c.relkind = 'S'
  AND t.relname = 'reports'
  AND t.relnamespace = 'lava_impact'::regnamespace;
-- Expected: 0 rows (reports uses TEXT PK, no serial columns)
-- If any rows → add ALTER SEQUENCE ... RENAME TO ... in migration.

-- 7. Verify no foreign keys referencing reports from other tables
SELECT conname, conrelid::regclass AS referencing_table
FROM pg_constraint
WHERE confrelid = 'lava_impact.reports'::regclass
  AND contype = 'f';
-- Expected: 0 rows

-- 8. Verify no triggers on the table
SELECT tgname FROM pg_trigger
WHERE tgrelid = 'lava_impact.reports'::regclass
  AND NOT tgisinternal;
-- Expected: 0 rows

-- 9. Verify no functions reference the table name in their body
SELECT proname FROM pg_proc
WHERE (prosrc ILIKE '%lava_impact.reports%' OR prosrc ILIKE '%FROM reports%')
  AND pronamespace = 'lava_impact'::regnamespace;
-- Expected: 0 rows (or only attribution_rank, which doesn't reference table)

-- 10. Verify no materialized views
SELECT matviewname FROM pg_matviews
WHERE schemaname = 'lava_impact';
-- Expected: 0 rows

-- 11. Verify no active connections (quiescence)
SELECT pid, usename, application_name, state, query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid != pg_backend_pid()
  AND state != 'idle';
-- Expected: 0 rows (no active queries besides your session)
-- If rows appear → stop those processes before proceeding.

-- 12. Record pre-migration row count for post-check
SELECT COUNT(*) FROM lava_impact.reports_public;
-- Save this number for post-migration verification.

-- 13. Capture current view definition for post-migration comparison
SELECT pg_get_viewdef('lava_impact.reports_public'::regclass, true);
-- Save this output. After migration, corpus_public definition must match
-- with only the table name changed (reports → corpus).
