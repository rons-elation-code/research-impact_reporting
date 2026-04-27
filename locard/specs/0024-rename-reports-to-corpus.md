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
instance and deploy from the same git branch. The operator controls when each
host pulls new code.

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
statements will error if run twice. The operator runs it once in a `psql`
session. If it fails partway through, the transaction rolls back (all statements
are inside `BEGIN`/`COMMIT`).

#### Preflight Checks

Before running migration 008, the operator runs these queries to confirm the
schema matches expectations:

```sql
-- Verify table exists
SELECT tablename FROM pg_tables
WHERE schemaname = 'lava_impact' AND tablename = 'reports';
-- Expected: 1 row

-- Verify all 20 constraints exist
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

-- Verify all 8 indexes exist
SELECT indexname FROM pg_indexes
WHERE schemaname = 'lava_impact' AND tablename = 'reports'
  AND indexname LIKE 'idx_reports_%'
ORDER BY indexname;
-- Expected (8 rows):
--   idx_reports_classification, idx_reports_discovered_via,
--   idx_reports_ein, idx_reports_event_type,
--   idx_reports_material_group, idx_reports_material_type,
--   idx_reports_platform, idx_reports_year

-- Verify view exists and has no dependent views
SELECT viewname FROM pg_views
WHERE schemaname = 'lava_impact' AND viewname = 'reports_public';
-- Expected: 1 row

SELECT dependent.relname
FROM pg_depend d
JOIN pg_rewrite r ON d.objid = r.oid
JOIN pg_class dependent ON r.ev_class = dependent.oid
JOIN pg_class source ON d.refobjid = source.oid
WHERE source.relname = 'reports_public'
  AND dependent.relname != 'reports_public';
-- Expected: 0 rows (no dependent views)
```

If any preflight check fails, do NOT proceed. Investigate the discrepancy first.

#### Migration SQL

```sql
-- 008_rename_reports_to_corpus.sql
-- ONE-SHOT migration. Run once. Rolls back on any error.
BEGIN;

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

-- 4. Drop old view, create new view with identical filtering semantics
DROP VIEW IF EXISTS lava_impact.reports_public;

CREATE VIEW lava_impact.corpus_public AS
  SELECT * FROM lava_impact.corpus
  WHERE attribution_confidence IN ('direct_link','scraped_page','sitemap')
    AND (classification_confidence IS NULL OR classification_confidence >= 0.8)
    AND pdf_has_javascript  = 0
    AND pdf_has_launch      = 0
    AND pdf_has_embedded    = 0
    AND pdf_has_uri_actions = 0;

COMMIT;
```

**View filtering semantics**: The `corpus_public` view WHERE clause MUST be
identical to the current `reports_public` view (from migration 007). This is the
security/quality boundary — it excludes low-confidence classifications,
unverified attributions, and PDFs with active content. The builder must verify
the WHERE clause matches by reading the current view definition before writing
migration 008.

#### Rollback SQL

If rollback is needed (e.g., code not yet deployed, want to revert DB):

```sql
BEGIN;
ALTER TABLE lava_impact.corpus RENAME TO reports;
-- (constraints/indexes reverse similarly)
DROP VIEW IF EXISTS lava_impact.corpus_public;
CREATE VIEW lava_impact.reports_public AS
  SELECT * FROM lava_impact.reports
  WHERE attribution_confidence IN ('direct_link','scraped_page','sitemap')
    AND (classification_confidence IS NULL OR classification_confidence >= 0.8)
    AND pdf_has_javascript  = 0
    AND pdf_has_launch      = 0
    AND pdf_has_embedded    = 0
    AND pdf_has_uri_actions = 0;
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
migration handles that separately). The Django `AlterModelTable` operation
produces `ALTER TABLE "reports" RENAME TO "corpus"` by default, but since we run
the RDS migration first, the table will already be renamed. To handle this:

- Option A: Use `migrations.SeparateDatabaseAndState` to make the Django
  migration state-only (no SQL emitted). This is the preferred approach.
- Option B: Run Django migrate before the RDS migration and let Django do the
  table rename (but then constraints/indexes/views are still not renamed).

**Recommended: Option A.** The RDS migration is the source of truth for all DB
changes. Django migration is state-only.

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

## Technical Implementation

### Migration Procedure (Strict Ordering)

All steps are performed by the single operator in one session:

1. **Verify no jobs running** — check dashboard, confirm all phases idle
2. **Stop dashboard and all pipeline processes** on all hosts
3. **Run preflight checks** — execute the preflight SQL queries above
4. **Run migration 008** — `psql -f 008_rename_reports_to_corpus.sql` against RDS
5. **Run post-migration verification** — execute the post-flight queries below
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

```sql
-- Table renamed
SELECT tablename FROM pg_tables
WHERE schemaname = 'lava_impact' AND tablename = 'corpus';
-- Expected: 1 row

-- Old table gone
SELECT tablename FROM pg_tables
WHERE schemaname = 'lava_impact' AND tablename = 'reports';
-- Expected: 0 rows

-- View works and returns same count
SELECT COUNT(*) FROM lava_impact.corpus_public;
-- Expected: same count as pre-migration reports_public

-- Old view gone
SELECT viewname FROM pg_views
WHERE schemaname = 'lava_impact' AND viewname = 'reports_public';
-- Expected: 0 rows

-- Spot-check a write path (optional, only if comfortable)
-- INSERT a test row, verify it appears, DELETE it
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
# Should return ZERO hits (excluding historical migrations and Python module paths)
grep -rn '\.reports[^_/]' lavandula/ --include="*.py" \
  | grep -v 'lavandula/reports/' \
  | grep -v '__pycache__'

# Also check for bare SQL table refs
grep -rn 'FROM reports\b\|INTO reports\b\|UPDATE reports\b\|JOIN reports\b' \
  lavandula/ --include="*.py" | grep -v __pycache__
# Expected: 0 hits

# Check for stale view refs
grep -rn 'reports_public' lavandula/ --include="*.py" | grep -v __pycache__
# Expected: 0 hits
```

**Exclusions**: Historical migrations (`lavandula/migrations/rds/001-007`), Python
module paths (`from lavandula.reports`), and URL paths (`/dashboard/reports/`) are
explicitly excluded from this check.

## Traps to Avoid

1. **Don't rename Python imports** — `from lavandula.reports.X` stays.
2. **Don't touch migration 001–007** — they are historical.
3. **Don't rename the `Report` class** — only `db_table` changes.
4. **In db_writer.py, don't blindly replace "reports"** — the docstring says
   "reports table" which should become "corpus table", but the import path
   `lavandula.reports.db_writer` must NOT change.
5. **Constraint count** — verify all 20 constraints exist before renaming.
   If a constraint is missing, the transaction will roll back. Investigate first.
6. **View WHERE clause** — `corpus_public` must have identical filtering to
   `reports_public`. This is the security boundary. Verify by reading the current
   view definition: `\d+ lava_impact.reports_public`.
7. **Don't deploy code before DB migration** — new code + old schema = immediate
   SQL errors. Run migration 008 first.
8. **Django migration is state-only** — use `SeparateDatabaseAndState` so Django
   doesn't try to rename an already-renamed table.

## Acceptance Criteria

- [ ] Preflight checks pass (20 constraints, 8 indexes, 1 view, 0 dependents)
- [ ] Migration 008 runs cleanly on RDS within a single transaction
- [ ] `\dt lava_impact.*` shows `corpus`, no `reports`
- [ ] `\dv lava_impact.*` shows `corpus_public`, no `reports_public`
- [ ] `SELECT COUNT(*) FROM lava_impact.corpus_public` matches pre-migration count
- [ ] All Python SQL references updated (grep checks above return zero hits)
- [ ] Django model `db_table = "corpus"` with state-only migration
- [ ] Dashboard Reports tab loads and shows correct row counts
- [ ] `pipeline_classify --limit 1` completes with exit code 0 and writes to `corpus`
- [ ] `python -m lavandula.reports.db_writer` (if testable) writes to `corpus`
- [ ] All existing unit tests pass
- [ ] No references to `reports_public` remain in Python code (outside historical migrations)
