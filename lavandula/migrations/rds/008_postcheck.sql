-- 008_postcheck.sql
-- Run ALL queries in PGAdmin AFTER 008_rename_reports_to_corpus.sql completes.
-- All checks must pass before deploying code.

-- Table renamed
SELECT tablename FROM pg_tables
WHERE schemaname = 'lava_impact' AND tablename = 'corpus';
-- Expected: 1 row

-- Old table gone
SELECT tablename FROM pg_tables
WHERE schemaname = 'lava_impact' AND tablename = 'reports';
-- Expected: 0 rows

-- All 20 constraints renamed
SELECT conname FROM pg_constraint
WHERE conrelid = 'lava_impact.corpus'::regclass
  AND conname LIKE 'corpus_%'
ORDER BY conname;
-- Expected: 20 rows, all starting with corpus_

-- No old constraint names remain
SELECT conname FROM pg_constraint
WHERE conrelid = 'lava_impact.corpus'::regclass
  AND conname LIKE 'reports_%';
-- Expected: 0 rows

-- All 8 indexes renamed
SELECT indexname FROM pg_indexes
WHERE schemaname = 'lava_impact' AND tablename = 'corpus'
  AND indexname LIKE 'idx_corpus_%'
ORDER BY indexname;
-- Expected: 8 rows

-- No old index names remain
SELECT indexname FROM pg_indexes
WHERE schemaname = 'lava_impact' AND tablename = 'corpus'
  AND indexname LIKE 'idx_reports_%';
-- Expected: 0 rows

-- View renamed and returns same count as pre-migration
SELECT COUNT(*) FROM lava_impact.corpus_public;
-- Expected: matches saved pre-migration count

-- View definition matches (table name substituted)
SELECT pg_get_viewdef('lava_impact.corpus_public'::regclass, true);
-- Expected: identical to pre-migration definition with reports → corpus

-- Old view gone
SELECT viewname FROM pg_views
WHERE schemaname = 'lava_impact' AND viewname = 'reports_public';
-- Expected: 0 rows

-- Nothing named 'reports' remains in lava_impact schema
SELECT relname FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'lava_impact' AND relname LIKE '%reports%';
-- Expected: 0 rows
