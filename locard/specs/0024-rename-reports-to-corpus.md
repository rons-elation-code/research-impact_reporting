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
backwards-compatibility shims. The migration is a controlled, offline operation.

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

A single idempotent SQL migration file:

```sql
-- 008_rename_reports_to_corpus.sql
BEGIN;

-- 1. Rename table
ALTER TABLE lava_impact.reports RENAME TO corpus;

-- 2. Rename constraints (17 constraints from 001 + 007)
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
-- From 007:
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_mt_chk TO corpus_mt_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_mg_chk TO corpus_mg_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_et_chk TO corpus_et_chk;

-- 3. Rename indexes (8 indexes from 001 + 005 + 007)
ALTER INDEX lava_impact.idx_reports_ein RENAME TO idx_corpus_ein;
ALTER INDEX lava_impact.idx_reports_classification RENAME TO idx_corpus_classification;
ALTER INDEX lava_impact.idx_reports_year RENAME TO idx_corpus_year;
ALTER INDEX lava_impact.idx_reports_platform RENAME TO idx_corpus_platform;
ALTER INDEX lava_impact.idx_reports_discovered_via RENAME TO idx_corpus_discovered_via;
ALTER INDEX lava_impact.idx_reports_material_type RENAME TO idx_corpus_material_type;
ALTER INDEX lava_impact.idx_reports_material_group RENAME TO idx_corpus_material_group;
ALTER INDEX lava_impact.idx_reports_event_type RENAME TO idx_corpus_event_type;

-- 4. Recreate views pointing at new table name
CREATE OR REPLACE VIEW lava_impact.corpus_public AS
  SELECT * FROM lava_impact.corpus
  WHERE attribution_confidence IN ('direct_link','scraped_page','sitemap')
    AND (classification_confidence IS NULL OR classification_confidence >= 0.8)
    AND pdf_has_javascript  = 0
    AND pdf_has_launch      = 0
    AND pdf_has_embedded    = 0
    AND pdf_has_uri_actions = 0;

-- 5. Drop old view
DROP VIEW IF EXISTS lava_impact.reports_public;

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

### Migration Procedure

Since this is a single-operator environment:

1. **Stop all pipeline processes** — no jobs running (verify via dashboard)
2. **Run migration 008** — `psql` against RDS, execute the SQL above
3. **Deploy code** — `git pull` on all hosts (cloud2, any others)
4. **Run Django migrate** — applies the `AlterModelTable` migration
5. **Verify** — dashboard loads, run a small crawl or classify job

No rollback plan needed beyond "rename back" — the SQL is trivially reversible
and the operator controls when it runs.

### Code Change Strategy

The bulk change is mechanical find-and-replace in SQL strings:
- `{_SCHEMA}.reports` → `{_SCHEMA}.corpus` (in f-strings)
- `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- `reports.column_name` → `corpus.column_name` (in UPSERT SET clauses)

**Risk: db_writer.py** — This file has 69 references and complex multi-line SQL
with UPSERT logic. The replacement must be precise: only change SQL table/column
qualifiers, not Python variable names or comment references to the module.

### Verification

- All existing tests pass (they use SQLite in-memory, so table name comes from
  Django model `db_table` which will be updated)
- Manual: dashboard Reports tab loads, shows correct counts
- Manual: run `pipeline_classify --limit 1` successfully

## Traps to Avoid

1. **Don't rename Python imports** — `from lavandula.reports.X` stays.
2. **Don't touch migration 001–007** — they are historical.
3. **Don't rename the `Report` class** — only `db_table` changes.
4. **In db_writer.py, don't blindly replace "reports"** — the docstring says
   "reports table" which should become "corpus table", but the import path
   `lavandula.reports.db_writer` must NOT change.
5. **Constraint count** — verify all 20 constraints exist before renaming.
   If a constraint was dropped or never created, the `RENAME CONSTRAINT` will
   error. Run `SELECT conname FROM pg_constraint WHERE conrelid = 'lava_impact.reports'::regclass;` first.

## Acceptance Criteria

- [ ] Migration 008 runs cleanly on RDS
- [ ] `\dt lava_impact.*` shows `corpus`, no `reports`
- [ ] `\dv lava_impact.*` shows `corpus_public`, no `reports_public`
- [ ] All Python SQL references updated (grep for `\.reports[^_/]` in lavandula/ returns zero hits excluding historical migrations and module paths)
- [ ] Django model `db_table = "corpus"`
- [ ] Dashboard Reports tab functions correctly
- [ ] `pipeline_classify --limit 1` runs successfully
- [ ] All existing tests pass
