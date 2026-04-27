# Plan 0024 — Rename `reports` Table to `corpus`

**Spec**: `locard/specs/0024-rename-reports-to-corpus.md`
**Protocol**: SPIDER

## Overview

This is a mechanical rename. The work divides into two independent tracks:

1. **SQL migration file** (operator runs in PGAdmin) — rename table, constraints,
   indexes, and view in a single transaction.
2. **Python code changes** — find-and-replace SQL string references, update Django
   model `db_table`, generate state-only Django migration.

The builder delivers both tracks plus the grep verification. The operator handles
the RDS snapshot and PGAdmin execution.

## Implementation Steps

### Step 1: Create RDS migration file

Create `lavandula/migrations/rds/008_rename_reports_to_corpus.sql` with the
exact SQL from the spec's "Migration SQL" section. This is a copy — do not
modify the SQL.

Also create `lavandula/migrations/rds/008_rollback.sql` with the rollback SQL
from the spec.

**Files**:
- `lavandula/migrations/rds/008_rename_reports_to_corpus.sql` (new)
- `lavandula/migrations/rds/008_rollback.sql` (new)

### Step 2: Update `db_writer.py` — SQL string references

This is the highest-risk file (69 references in complex UPSERT SQL).

**What to change**: Every occurrence of `{_SCHEMA}.reports.` and
`{_SCHEMA}.reports` (as a table name in SQL context) must become
`{_SCHEMA}.corpus.` / `{_SCHEMA}.corpus`.

Specific patterns in the `_UPSERT_REPORT_SQL` constant (lines 183–376):
- Line 184: `INSERT INTO {_SCHEMA}.reports (` → `INSERT INTO {_SCHEMA}.corpus (`
- Lines 215–375: All `{_SCHEMA}.reports.column_name` → `{_SCHEMA}.corpus.column_name`
  in the `ON CONFLICT ... DO UPDATE SET` clause. There are ~40 of these.

Also update the module docstring (lines 15–16):
- `lava_impact.reports` → `lava_impact.corpus`
- `lava_impact.reports_public` → `lava_impact.corpus_public`

And the docstring on `upsert_report` (line 415):
- `lava_impact.reports` → `lava_impact.corpus`

**What NOT to change**:
- `_SCHEMA = "lava_impact"` — stays
- Any `from lavandula.reports` import paths — stays
- References to other tables (`fetch_log`, `crawled_orgs`, `deletion_log`, `runs`) — stays

**Technique**: Use `replace_all` with `{_SCHEMA}.reports.` → `{_SCHEMA}.corpus.`
(with trailing dot, catches all column qualifiers). Then separately fix
`INSERT INTO {_SCHEMA}.reports (` → `INSERT INTO {_SCHEMA}.corpus (`.

**File**: `lavandula/reports/db_writer.py`

### Step 3: Update `catalogue.py` — SQL string references

6 references to change:

- Line 3: docstring `lava_impact.reports_public` → `lava_impact.corpus_public`
- Line 4: docstring `reports` table → `corpus` table
- Line 6: docstring `from reports` → `from corpus`
- Line 25: docstring `reports_public` → `corpus_public`
- Line 28: `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- Line 39: `{_SCHEMA}.reports` → `{_SCHEMA}.corpus`
- Line 73: `{_SCHEMA}.reports` → `{_SCHEMA}.corpus`
- Line 116: `{_SCHEMA}.reports` → `{_SCHEMA}.corpus`

**File**: `lavandula/reports/catalogue.py`

### Step 4: Update `report.py` — SQL string references

7 references to change:

- Line 3: docstring `lava_impact.reports_public` → `lava_impact.corpus_public`
- Line 38: `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- Line 42: `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- Line 48: `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- Line 54: `{_SCHEMA}.reports_public` → `{_SCHEMA}.corpus_public`
- Line 75: body string `` `reports_public` `` → `` `corpus_public` ``
- Line 94: body string `` `reports_public` `` → `` `corpus_public` ``

**File**: `lavandula/reports/report.py`

### Step 5: Update `schema.py` — test helper table reference

2 references to change:

- Line 41: docstring `reports` → `corpus`
- Line 57: `f"{schema}.reports"` → `f"{schema}.corpus"` and
  `"reports"` → `"corpus"` (the no-schema fallback)

**File**: `lavandula/reports/schema.py`

### Step 6: Update `classify.py` and `__init__.py` — docstrings only

**classify.py** line 8: `reports_public` → `corpus_public`

**__init__.py** line 5: `reports_public` → `corpus_public`

**Files**:
- `lavandula/reports/classify.py`
- `lavandula/reports/__init__.py`

### Step 7: Update Django model `db_table`

Change `lavandula/dashboard/pipeline/models.py` line 42:
```python
db_table = "reports"  →  db_table = "corpus"
```

**File**: `lavandula/dashboard/pipeline/models.py`

### Step 8: Create state-only Django migration

Create a new migration file in `lavandula/dashboard/pipeline/migrations/`.
Use `SeparateDatabaseAndState` so no SQL is emitted:

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0002_partial_unique_indexes"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelTable(
                    name="report",
                    table="corpus",
                ),
            ],
            database_operations=[],
        ),
    ]
```

**File**: `lavandula/dashboard/pipeline/migrations/0003_rename_reports_to_corpus.py` (new)

### Step 9: Update test docstrings

- `lavandula/reports/tests/unit/test_classify.py` line 115:
  `reports_public` → `corpus_public`
- `lavandula/reports/tests/unit/test_s3_archive_0007.py` line 91:
  `reports_public` → `corpus_public`

**Files**:
- `lavandula/reports/tests/unit/test_classify.py`
- `lavandula/reports/tests/unit/test_s3_archive_0007.py`

### Step 10: Run grep verification

Execute the three grep checks from the spec's "Grep-Based Completion Check"
section. All must return 0 hits.

### Step 11: Run tests

Run the existing test suite to confirm nothing broke:
```bash
python -m pytest lavandula/reports/tests/ -v
```

## Testing Strategy

- **Unit tests**: Run existing test suite. Tests use SQLite in-memory via Django
  `db_table`, which will be updated to `"corpus"` in Step 7. If any test creates
  tables named `reports` explicitly, it will need updating (schema.py's
  `insert_raw_report_for_test` handles this — updated in Step 5).
- **Grep verification**: Mechanical check that no stale SQL references remain.
- **Manual (operator)**: After RDS migration, run dashboard and `pipeline_classify --limit 1`.

## Files Changed

| File | Type | Step |
|------|------|------|
| `lavandula/migrations/rds/008_rename_reports_to_corpus.sql` | New | 1 |
| `lavandula/migrations/rds/008_rollback.sql` | New | 1 |
| `lavandula/reports/db_writer.py` | Edit | 2 |
| `lavandula/reports/catalogue.py` | Edit | 3 |
| `lavandula/reports/report.py` | Edit | 4 |
| `lavandula/reports/schema.py` | Edit | 5 |
| `lavandula/reports/classify.py` | Edit | 6 |
| `lavandula/reports/__init__.py` | Edit | 6 |
| `lavandula/dashboard/pipeline/models.py` | Edit | 7 |
| `lavandula/dashboard/pipeline/migrations/0003_rename_reports_to_corpus.py` | New | 8 |
| `lavandula/reports/tests/unit/test_classify.py` | Edit | 9 |
| `lavandula/reports/tests/unit/test_s3_archive_0007.py` | Edit | 9 |

## Risk Assessment

**db_writer.py** is the only high-risk file. It has 69 SQL references in a
complex UPSERT statement. The replace-all approach (`{_SCHEMA}.reports.` →
`{_SCHEMA}.corpus.`) is safe because:
1. The trailing dot ensures only SQL column qualifiers match, not import paths.
2. The `INSERT INTO {_SCHEMA}.reports (` pattern is unique and unambiguous.
3. No other table name starts with `reports` in this schema.

Everything else is straightforward: view name swaps, docstring updates, and a
one-line Django model change.

## Acceptance Criteria

Per spec. The builder is responsible for steps 1–11. The operator handles the
RDS snapshot, PGAdmin execution, and post-deploy verification.
