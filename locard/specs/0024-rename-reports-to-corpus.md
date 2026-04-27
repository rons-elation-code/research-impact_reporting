# Spec 0024 — Rename `reports` Table to `corpus`

**Status**: Draft
**Priority**: Medium
**Release**: Unassigned

## Problem Statement

The `lava_impact.reports` table was named when the project only collected annual
reports. The table now stores annual reports, impact reports, hybrids, event
materials, and other document types — all classified by the LLM classifier. The
name `reports` is misleading and will become more so as we add document types.

Renaming to `corpus` accurately describes the table's role: a classified corpus
of nonprofit documents indexed by content hash.

## Operating Context

**This is a single-operator system.** One person (ronp) uses the database. There
are no other users, services, or applications reading/writing concurrently. This
eliminates zero-downtime migration concerns, dual-write patterns, and
backwards-compatibility shims. The migration is a controlled, offline operation
run from a single terminal session.

All hosts (t3.small dev, t3.medium crawl, g6 resolver) share the same RDS
instance (`lava_prod1`) and deploy from the same git branch. The operator
controls when each host pulls new code.

**Database access**: The operator manages RDS via PGAdmin. All migration SQL
must be provided as PGAdmin-compatible scripts (standard SQL, no `psql`
meta-commands like `\dt` or `\dv`). Verification queries must also be
PGAdmin-compatible.

## Goals

1. Rename the PostgreSQL table `lava_impact.reports` → `lava_impact.corpus`
2. Rename the view `lava_impact.reports_public` → `lava_impact.corpus_public`
3. Rename all constraints `reports_*` → `corpus_*`
4. Rename all indexes `idx_reports_*` → `idx_corpus_*`
5. Update all Python code that generates SQL referencing `reports` / `reports_public`
6. Update the Django model `db_table` from `"reports"` to `"corpus"`
7. Create a Django migration for the `db_table` change
8. Do NOT touch historical migration files (001–007) — they are already applied
9. Do NOT rename the Python module `lavandula/reports/` or URL paths `/dashboard/reports/`
10. Do NOT rename the Python class `Report` or the `report.py` module

## Scope

### In Scope — RDS Migration (new migration 008)

A single **one-shot** SQL migration file. This is NOT idempotent — `RENAME`
statements will error if run twice. The operator runs it once via PGAdmin query
tool. If it fails partway through, the transaction rolls back (all statements
are inside `BEGIN`/`COMMIT`).

#### Preflight Checks

Before running migration 008, the operator runs these queries in PGAdmin to
confirm the schema matches expectations:

```sql
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
```

If any preflight check fails, do NOT proceed. Investigate the discrepancy first.

#### Migration SQL

```sql
-- 008_rename_reports_to_corpus.sql
-- ONE-SHOT migration. Run once in PGAdmin. Rolls back on any error.
-- DO NOT RUN without completing preflight checks above.
BEGIN;

SET LOCAL lock_timeout = '5s';

-- 1. Rename table
ALTER TABLE lava_impact.reports RENAME TO corpus;

-- 2. Rename constraints (20 total: 17 from 001 + 3 from 007)
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_sha_len_chk TO corpus_sha_len_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_size_chk TO corpus_size_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_ct_chk TO corpus_ct_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_disc_chk TO corpus_disc_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_platform_chk TO corpus_platform_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_class_chk TO corpus_class_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_conf_chk TO corpus_conf_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_attr_chk TO corpus_attr_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_redirect_chk TO corpus_redirect_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_js_chk TO corpus_js_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_launch_chk TO corpus_launch_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_embed_chk TO corpus_embed_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_uri_chk TO corpus_uri_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_fpt_len_chk TO corpus_fpt_len_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_creator_chk TO corpus_creator_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_producer_chk TO corpus_producer_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_year_src_chk TO corpus_year_src_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_mt_chk TO corpus_mt_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_mg_chk TO corpus_mg_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_et_chk TO corpus_et_chk;

-- 3. Rename indexes (8 total: 4 from 001 + 1 from 005 + 3 from 007)
ALTER INDEX lava_impact.idx_reports_ein RENAME TO idx_corpus_ein;
ALTER INDEX lava_impact.idx_reports_classification RENAME TO idx_corpus_classification;
ALTER INDEX lava_impact.idx_reports_year RENAME TO idx_corpus_year;
ALTER INDEX lava_impact.idx_reports_platform RENAME TO idx_corpus_platform;
ALTER INDEX lava_impact.idx_reports_discovered_via RENAME TO idx_corpus_discovered_via;
ALTER INDEX lava_impact.idx_reports_material_type RENAME TO idx_corpus_material_type;
ALTER INDEX lava_impact.idx_reports_material_group RENAME TO idx_corpus_material_group;
ALTER INDEX lava_impact.idx_reports_event_type RENAME TO idx_corpus_event_type;

-- 4. Rename view (preserves owner, grants, and filtering semantics automatically)
ALTER VIEW lava_impact.reports_public RENAME TO corpus_public;

COMMIT;
```

**Why `ALTER VIEW ... RENAME TO`**: PostgreSQL stores view definitions by OID,
not table name. After `ALTER TABLE reports RENAME TO corpus`, the view's internal
reference is automatically updated. `ALTER VIEW ... RENAME TO` is atomic,
preserves the owner, preserves all grants, and eliminates any risk of
WHERE-clause drift from manual transcription. This is strictly better than
DROP + CREATE.

#### Rollback SQL

If rollback is needed (e.g., code not yet deployed, want to revert DB).
**DO NOT RUN unless code has been rolled back to pre-spec-0024 state.**

```sql
BEGIN;

SET LOCAL lock_timeout = '5s';

-- Reverse view rename (must happen before table rename)
ALTER VIEW lava_impact.corpus_public RENAME TO reports_public;

-- Reverse table rename
ALTER TABLE lava_impact.corpus RENAME TO reports;

-- Reverse constraint renames
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_sha_len_chk TO reports_sha_len_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_size_chk TO reports_size_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_ct_chk TO reports_ct_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_disc_chk TO reports_disc_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_platform_chk TO reports_platform_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_class_chk TO reports_class_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_conf_chk TO reports_conf_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_attr_chk TO reports_attr_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_redirect_chk TO reports_redirect_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_js_chk TO reports_js_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_launch_chk TO reports_launch_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_embed_chk TO reports_embed_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_uri_chk TO reports_uri_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_fpt_len_chk TO reports_fpt_len_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_creator_chk TO reports_creator_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_producer_chk TO reports_producer_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_year_src_chk TO reports_year_src_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_mt_chk TO reports_mt_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_mg_chk TO reports_mg_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_et_chk TO reports_et_chk;

-- Reverse index renames
ALTER INDEX lava_impact.idx_corpus_ein RENAME TO idx_reports_ein;
ALTER INDEX lava_impact.idx_corpus_classification RENAME TO idx_reports_classification;
ALTER INDEX lava_impact.idx_corpus_year RENAME TO idx_reports_year;
ALTER INDEX lava_impact.idx_corpus_platform RENAME TO idx_reports_platform;
ALTER INDEX lava_impact.idx_corpus_discovered_via RENAME TO idx_reports_discovered_via;
ALTER INDEX lava_impact.idx_corpus_material_type RENAME TO idx_reports_material_type;
ALTER INDEX lava_impact.idx_corpus_material_group RENAME TO idx_reports_material_group;
ALTER INDEX lava_impact.idx_corpus_event_type RENAME TO idx_reports_event_type;

COMMIT;
```

### In Scope — Python Code Changes

Files with SQL string references that must change `reports` → `corpus` and
`reports_public` → `corpus_public`:

| File | References | Notes |
|------|-----------|-------|
| `lavandula/reports/db_writer.py` | ~69 | Heaviest file. All `INSERT INTO reports`, `reports.column` refs in UPSERT |
| `lavandula/reports/catalogue.py` | ~6 | `reports_public` reads + `DELETE FROM reports` |
| `lavandula/reports/report.py` | ~7 | `reports_public` aggregates + docstring |
| `lavandula/reports/schema.py` | ~2 | Test helper `insert_stub_row` table ref |
| `lavandula/reports/classify.py` | ~1 | Docstring only |
| `lavandula/reports/__init__.py` | ~2 | Docstring only |

### In Scope — Django Changes

| File | Change |
|------|--------|
| `lavandula/dashboard/pipeline/models.py:42` | `db_table = "reports"` → `db_table = "corpus"` |
| `lavandula/dashboard/pipeline/migrations/` | New migration: `AlterModelTable` for Report model |

The Django migration is **metadata-only** — it tells Django the table is now
called `corpus`. It does NOT generate any SQL to rename the table (the RDS
migration handles that separately). Use `migrations.SeparateDatabaseAndState`
so Django's `migrate` command only updates its internal state without emitting
any SQL (avoiding a double-rename error on the already-renamed table).

### In Scope — Tests

Test files referencing `reports` or `reports_public` in SQL or assertions:

| File | Notes |
|------|-------|
| `lavandula/reports/tests/unit/test_classify.py` | `reports_public` reference in docstring |
| `lavandula/reports/tests/unit/test_s3_archive_0007.py` | `reports_public` comment |

These are docstring/comment refs — they should be updated to say `corpus_public`
but won't break if missed.

### Out of Scope

- **Python module path** `lavandula/reports/` — stays as-is. "Reports" is still
  a reasonable module name for the crawler/classifier subsystem.
- **URL paths** `/dashboard/reports/` — no user-facing change needed.
- **Python class** `Report` in `pipeline/models.py` — the Django model name is
  fine; only `db_table` changes.
- **Historical migrations** (001–007) — already applied, never re-run.
- **SQLite** — if any local SQLite references remain, they are legacy and out of scope.
- **Fresh-environment bootstrap** — migrations 001–007 still create a `reports`
  table. A fresh RDS setup would need to run 001–007 then 008. This is acceptable
  since there is only one RDS instance and no plan to recreate it from scratch.
  If a fresh bootstrap is ever needed, run all migrations in order.
- **`corpus_public` column list** — the view currently uses `SELECT *`. A
  follow-up spec could tighten this to explicit columns, but that's a separate
  concern from the rename.

## Technical Implementation

### Migration Procedure (Strict Ordering)

All steps are performed by the single operator in one session:

0. **Take RDS snapshot** — via AWS console. Record snapshot identifier. This is
   the ultimate safety net independent of transactional rollback.
1. **Verify no jobs running** — check dashboard, confirm all phases idle
2. **Stop dashboard and all pipeline processes** on all hosts
3. **Run preflight checks** — execute the preflight SQL queries in PGAdmin
4. **Run migration 008** — paste the migration SQL into PGAdmin query tool
5. **Run post-migration verification** — execute the post-flight queries in PGAdmin
6. **Deploy code** — `git pull` on all hosts
7. **Run Django migrate** — `python manage.py migrate` (applies state-only migration)
8. **Start dashboard**
9. **Verify** — dashboard Reports tab loads, counts match pre-migration counts

**Important**: Code must NOT be deployed before the DB migration. The new code
references `corpus`; the old DB has `reports`. Running new code against old DB
will produce immediate SQL errors. Conversely, old code against new DB will also
fail. The operator controls both, so this is simply: run SQL first, deploy code
second, do both before starting any processes.

### Post-Migration Verification

Run these queries in PGAdmin after migration 008 completes:

```sql
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
```

### Code Change Strategy

The bulk change is mechanical find-and-replace in SQL strings:
- `{_SCHEMA}.reports` → `{_SCHEMA}.corpus` (in f-strings)
- `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- `reports.column_name` → `corpus.column_name` (in UPSERT SET clauses)

**Risk: db_writer.py** — This file has 69 references and complex multi-line SQL
with UPSERT logic. The replacement must be precise: only change SQL table/column
qualifiers, not Python variable names or comment references to the module.

### Grep-Based Completion Check

After all code changes, run:

```bash
# Check for stale SQL table references IN the reports module (the main target)
grep -rn 'INTO reports\b\|FROM reports\b\|UPDATE reports\b\|JOIN reports\b\|\.reports\b' \
  lavandula/reports/ --include="*.py" \
  | grep -v '__pycache__' \
  | grep -v 'from lavandula\.reports' \
  | grep -v 'import.*reports'
# Expected: 0 hits

# Check for stale view refs everywhere
grep -rn 'reports_public' lavandula/ --include="*.py" | grep -v __pycache__
# Expected: 0 hits

# Check for stale SQL refs outside the reports module
grep -rn 'INTO reports\b\|FROM reports\b\|UPDATE reports\b\|JOIN reports\b' \
  lavandula/ --include="*.py" \
  | grep -v 'lavandula/reports/' \
  | grep -v __pycache__
# Expected: 0 hits
```

**Exclusions**: Historical migrations (`lavandula/migrations/rds/001-007`) and
Python import paths (`from lavandula.reports`) are explicitly excluded.

## Traps to Avoid

1. **Don't rename Python imports** — `from lavandula.reports.X` stays.
2. **Don't touch migration 001–007** — they are historical.
3. **Don't rename the `Report` class** — only `db_table` changes.
4. **In db_writer.py, don't blindly replace "reports"** — the docstring says
   "reports table" which should become "corpus table", but the import path
   `lavandula.reports.db_writer` must NOT change.
5. **Constraint count** — verify all 20 constraints exist before renaming.
   If a constraint is missing, the transaction will roll back. Investigate first.
6. **Don't deploy code before DB migration** — new code + old schema = immediate
   SQL errors. Run migration 008 first.
7. **Django migration is state-only** — use `SeparateDatabaseAndState` so Django
   doesn't try to rename an already-renamed table.

## Acceptance Criteria

- [ ] RDS snapshot taken before migration
- [ ] Preflight checks pass (20 constraints, 8 indexes, 0 sequences, 0 FKs,
      0 triggers, 0 dependent views, 0 active connections)
- [ ] Migration 008 runs cleanly on RDS within a single transaction
- [ ] `SELECT tablename FROM pg_tables WHERE schemaname='lava_impact'` shows `corpus`, no `reports`
- [ ] `SELECT viewname FROM pg_views WHERE schemaname='lava_impact'` shows `corpus_public`, no `reports_public`
- [ ] `SELECT COUNT(*) FROM lava_impact.corpus_public` matches pre-migration count
- [ ] `pg_get_viewdef('lava_impact.corpus_public')` matches pre-migration definition (table name substituted)
- [ ] All Python SQL references updated (grep checks above return zero hits)
- [ ] Django model `db_table = "corpus"` with state-only migration
- [ ] Dashboard Reports tab loads and shows correct row counts
- [ ] `pipeline_classify --limit 1` completes with exit code 0 and writes to `corpus`
- [ ] All existing unit tests pass
- [ ] No references to `reports_public` remain in Python code (outside historical migrations)
