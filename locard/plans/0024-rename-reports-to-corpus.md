# Plan 0024 — Rename `reports` Table to `corpus`

**Spec**: `locard/specs/0024-rename-reports-to-corpus.md`
**Protocol**: SPIDER

## Overview

This is a mechanical rename. The work divides into two tracks:

1. **SQL migration package** (operator runs in PGAdmin) — preflight checks,
   migration, postflight verification, and rollback scripts.
2. **Python code changes** — find-and-replace SQL string references, update
   Django model `db_table`, generate state-only Django migration.

### Responsibility Split

**Builder delivers** (steps 1–11): All code changes, SQL files committed to
repo, grep verification, test suite run.

**Operator handles** (after builder's PR merges):

1. RDS snapshot (record identifier). **Wait until snapshot status = `available`**
   before proceeding — snapshot creation is async.
2. Stop all processes on all hosts
3. Run `008_preflight.sql` in PGAdmin — verify all checks pass.
   Re-run the connection check (#11) immediately before step 4 to close the
   TOCTOU window. Consider checking ALL connections (not just non-idle) to
   confirm no pool connections remain.
4. Run `008_rename_reports_to_corpus.sql` in PGAdmin
5. Run `008_postcheck.sql` in PGAdmin — verify all checks pass
6. `git pull` on all hosts
7. `python manage.py migrate` on the dashboard host
8. Start dashboard
9. Run `pipeline_classify --limit 1` — verify exit code 0 and writes to `corpus`
10. Verify dashboard Reports tab shows correct counts

The strict ordering is: **DB migration first, code deploy second** — new code
references `corpus`; deploying against old `reports` schema will cause immediate
SQL errors.

**Failure handling**: If step 6 or 7 fails after the DB has been renamed (step 4),
the system is down until resolved. The operator must: (a) fix the deploy/migrate
issue, OR (b) revert code to pre-0024 state on all hosts, THEN run
`008_rollback.sql` to restore the old schema. Do NOT run rollback SQL while new
code is deployed — that creates the inverse mismatch.

### Guardrail: Historical Migrations

Migrations 001–007 in `lavandula/migrations/rds/` are already applied and MUST
NOT be modified. The builder must not touch these files during grep-based
replacements or any other step.

## Implementation Steps

### Step 1: Create SQL migration package

Create four files in `lavandula/migrations/rds/`:

**`008_preflight.sql`** — Copy the 13 preflight queries from the spec's
"Preflight Checks" section. These are run by the operator in PGAdmin before the
migration. Include expected results as comments.

**`008_rename_reports_to_corpus.sql`** — Copy verbatim from the spec's "Migration
SQL" section, including `BEGIN`, `SET LOCAL lock_timeout = '5s'`, and `COMMIT`.
Do not modify.

**`008_postcheck.sql`** — Copy the post-migration verification queries from the
spec's "Post-Migration Verification" section. Include expected results as
comments.

**`008_rollback.sql`** — Copy the rollback SQL from the spec. Add a header
comment: `-- DO NOT RUN unless code has been rolled back to pre-spec-0024 state.`

**Files** (all new):
- `lavandula/migrations/rds/008_preflight.sql`
- `lavandula/migrations/rds/008_rename_reports_to_corpus.sql`
- `lavandula/migrations/rds/008_postcheck.sql`
- `lavandula/migrations/rds/008_rollback.sql`

### Step 2: Update `db_writer.py` — SQL string references

This is the highest-risk file (~69 references in complex UPSERT SQL).

**Before starting**: Count current references:
```bash
grep -c '{_SCHEMA}.reports' lavandula/reports/db_writer.py
```
Record this number.

**What to change**: Every SQL-context occurrence of `{_SCHEMA}.reports` must
become `{_SCHEMA}.corpus`. This includes:

1. **Column qualifiers** (trailing dot): `{_SCHEMA}.reports.column_name` →
   `{_SCHEMA}.corpus.column_name` (~40 occurrences in the UPSERT SET clause)
2. **INSERT target**: `INSERT INTO {_SCHEMA}.reports (` → `INSERT INTO {_SCHEMA}.corpus (`
3. **Docstrings**: `lava_impact.reports` → `lava_impact.corpus`,
   `lava_impact.reports_public` → `lava_impact.corpus_public`

**Technique**: Use `replace_all` on the string `{_SCHEMA}.reports` → `{_SCHEMA}.corpus`.
This catches ALL patterns — column qualifiers (`{_SCHEMA}.reports.foo`), INSERT
targets (`{_SCHEMA}.reports (`), and any other occurrences. It is safe because:
- The only `{_SCHEMA}` references in this file are to SQL table names
- No Python import paths use `{_SCHEMA}` syntax
- Other tables (`crawled_orgs`, `fetch_log`, etc.) don't contain `reports`

**After replacing**: Verify count drops to zero:
```bash
grep -c '{_SCHEMA}.reports' lavandula/reports/db_writer.py
# Expected: 0
```

Also update docstring references to `lava_impact.reports` and
`lava_impact.reports_public` (lines 15–16, line 415).

**What NOT to change**:
- `_SCHEMA = "lava_impact"` — stays
- Any `from lavandula.reports` import paths — stays (none in this file)
- References to other tables (`fetch_log`, `crawled_orgs`, `deletion_log`, `runs`) — stays

**File**: `lavandula/reports/db_writer.py`

### Step 3: Update `catalogue.py` — SQL string references

8 references to change:

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

**First**: Verify the latest existing migration:
```bash
ls lavandula/dashboard/pipeline/migrations/
```
Confirm `0002_partial_unique_indexes.py` is still the latest. Adjust the
dependency and filename if a newer migration exists.

Create a new migration using `SeparateDatabaseAndState` so no SQL is emitted:

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
section. All must return 0 hits. This is the **primary correctness signal** for
the code changes — unit tests under SQLite won't exercise the schema-qualified
SQL strings.

```bash
# Check for stale SQL table references IN the reports module
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

# Verify historical migrations were NOT modified
git diff --name-only lavandula/migrations/rds/001_initial_schema.sql \
  lavandula/migrations/rds/002_attribution_helper.sql \
  lavandula/migrations/rds/003_resolver_updated_at.sql \
  lavandula/migrations/rds/004_crawled_orgs_status_attempts.sql \
  lavandula/migrations/rds/005_wayback_provenance.sql \
  lavandula/migrations/rds/006_wayback_attribution_values.sql \
  lavandula/migrations/rds/007_classifier_expansion.sql
# Expected: no output (no changes to historical migrations)
```

### Step 11: Run tests

```bash
python -m pytest lavandula/ -v
```

Run the **full** test suite, not just the reports subset. Tests use SQLite
in-memory, so they verify that `schema.py`'s `insert_raw_report_for_test` and
Django's `db_table` are consistent, but they do NOT exercise the
schema-qualified SQL in `db_writer.py` or `catalogue.py` (those are
Postgres-specific). The grep verification in Step 10 is the primary correctness
check for SQL strings; tests catch Python-level breakage.

## Testing Strategy

- **Grep verification** (Step 10): Primary correctness signal for **code
  changes**. Confirms no stale `reports` SQL references remain in Python files.
- **Unit tests** (Step 11): Confirms Python-level consistency (Django model,
  test helper table names). Does NOT exercise Postgres-specific SQL strings.
- **Operator DB-side postcheck** (`008_postcheck.sql`): The authoritative
  schema-completeness check. The `SELECT relname ... LIKE '%reports%'` query
  catches any lingering DB objects the grep can't see.
- **Operator end-to-end** (`pipeline_classify --limit 1`): The true verification
  that SQL strings work against the renamed RDS table. This is a **required**
  acceptance gate, not optional. Dashboard Reports tab confirms the view works.

## Files Changed

| File | Type | Step |
|------|------|------|
| `lavandula/migrations/rds/008_preflight.sql` | New | 1 |
| `lavandula/migrations/rds/008_rename_reports_to_corpus.sql` | New | 1 |
| `lavandula/migrations/rds/008_postcheck.sql` | New | 1 |
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

**db_writer.py** is the only high-risk file. The `replace_all` on
`{_SCHEMA}.reports` → `{_SCHEMA}.corpus` catches all SQL patterns (column
qualifiers, INSERT targets, etc.) because:
1. The `{_SCHEMA}` prefix is only used for SQL table references in this file.
2. No Python import paths use `{_SCHEMA}` syntax.
3. The before/after grep count verifies completeness.

Everything else is straightforward: view name swaps, docstring updates, and a
one-line Django model change.

## Acceptance Criteria

Per spec. See "Responsibility Split" above for which criteria are verified by
the builder vs. the operator.
