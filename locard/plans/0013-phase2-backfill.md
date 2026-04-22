# Plan 0013 Phase 2 — SQLite → RDS Backfill Tool

**Spec**: `locard/specs/0013-rds-postgres-migration.md` Phase 2  
**Depends on**: Phase 1 (merged PR #6) + Phase 0 schema live on `lava_prod1.lava_impact`  
**Date**: 2026-04-22

---

## Scope

Ship one new tool and its tests. Zero changes to crawler / classifier /
resolver hot paths. Phase 3 (dual-write) and Phase 4 (read flip) are
explicitly NOT in this PR.

---

## Deliverables

| Path | Status |
|------|--------|
| `lavandula/common/tools/backfill_rds.py` | NEW — CLI tool |
| `lavandula/common/tools/__init__.py` | NEW (empty) |
| `lavandula/common/tests/unit/test_backfill_rds_0013.py` | NEW |

No schema changes. No adapter changes.

---

## Existing code to read first

1. `lavandula/common/db.py` — use `make_app_engine()` for the Postgres
   destination. **Note**: `app_user1` has CRUD on `lava_impact`,
   which is sufficient for INSERTs. The tool does NOT need master
   credentials.
2. `lavandula/reports/schema.py` — source-of-truth SQLite column
   names for `reports`, `fetch_log`, `crawled_orgs`, `deletion_log`,
   `budget_ledger`.
3. `lavandula/nonprofits/tools/seed_enumerate.py` — SQLite column
   names for `nonprofits_seed` and `runs` (plus the `_apply_migrations`
   list of post-initial columns).
4. `lavandula/migrations/rds/001_initial_schema.sql` — target
   Postgres column layout (should be 1:1 with SQLite column names).

---

## Command-line interface

```
python -m lavandula.common.tools.backfill_rds \
    --source-sqlite PATH \
    [--table TABLE_NAME]... \
    [--batch-size N] \
    [--schema lava_impact] \
    (--dry-run | --apply)
```

| Flag | Purpose | Default |
|------|---------|---------|
| `--source-sqlite` | Source SQLite DB file | required |
| `--table` | Specific table(s) to backfill; repeatable | all known tables |
| `--batch-size` | Rows per `execute_values` call | 1000 |
| `--schema` | Target Postgres schema | `lava_impact` |
| `--dry-run` | Count and compare rows; write nothing | mutually exclusive with `--apply` |
| `--apply` | Perform the inserts | mutually exclusive with `--dry-run` |

Exactly one of `--dry-run` / `--apply` is required — mirrors the
convention from `reconcile_s3.py` (spec 0007).

---

## Table mappings

Seven tables. Two mappings matter:

### A. Explicit primary key — idempotent via `ON CONFLICT DO NOTHING`

| SQLite table | PK column | Postgres DDL notes |
|-------------|-----------|-------------------|
| `nonprofits_seed` | `ein` | `ON CONFLICT (ein) DO NOTHING` |
| `reports` | `content_sha256` | `ON CONFLICT (content_sha256) DO NOTHING` |
| `crawled_orgs` | `ein` | `ON CONFLICT (ein) DO NOTHING` |
| `runs` | `run_id` | `ON CONFLICT (run_id) DO NOTHING` |

### B. Auto-increment PK — drop source IDs, let Postgres assign

| SQLite table | SQLite PK | Strategy |
|-------------|-----------|----------|
| `fetch_log` | `id BIGSERIAL` | Skip source `id` column; insert all other columns; Postgres auto-assigns new `id` |
| `deletion_log` | `id BIGSERIAL` | Same |
| `budget_ledger` | `id BIGSERIAL` | Same |

**Rationale**: no foreign key references these ids, so renumbering is safe. Source-id preservation would require `ALTER SEQUENCE ... SET`
gymnastics that add complexity for no benefit.

**Consequence**: re-running backfill against the same SQLite DB would
produce duplicate log entries in Postgres. To prevent this, the tool
emits a warning when the destination already contains rows for an
auto-id table, and requires `--apply-duplicates-ok` to proceed anyway.
Default behavior: skip auto-id tables if Postgres already has rows.

---

## Column source-of-truth

For each SQLite table, the tool reads `PRAGMA table_info(<table>)` at
runtime to get actual column names — that handles the additive
migrations applied over time (e.g., `nonprofits_seed` has
`resolver_status`, `address`, etc. added via `_apply_migrations`).

For the destination, the tool issues `SELECT column_name FROM
information_schema.columns WHERE table_schema = ? AND table_name = ?`
to get the actual target columns.

**Column alignment**: the tool intersects source + target columns and
inserts only the intersection. Columns present only on one side are
logged at INFO.

---

## Data-type coercion

SQLite → Postgres mapping at row level:

- `TEXT` → `str` (pass-through)
- `INTEGER` stored as bool (0/1) → `int` (Postgres SMALLINT auto-coerces)
- `INTEGER` stored as int → `int` (Postgres INTEGER/BIGINT auto-coerces)
- `REAL` → `float`
- `NULL` → `None`
- All timestamps stay as `TEXT` (SQLite storage is ISO 8601 string;
  Postgres target columns are `TEXT` in our schema, not `TIMESTAMP`)

No explicit conversion needed for most columns; psycopg2's default
type adapter handles `str`/`int`/`float`/`None` natively.

---

## Insert path (the performance-sensitive bit)

Use `psycopg2.extras.execute_values` for batched inserts:

```python
from psycopg2.extras import execute_values

sql = f"""
INSERT INTO {schema}.{table} ({col_list})
VALUES %s
ON CONFLICT ({pk_col}) DO NOTHING
"""
# For auto-id tables, omit the ON CONFLICT clause entirely.

with engine.connect() as conn:
    raw_conn = conn.connection  # psycopg2 connection
    with raw_conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=batch_size)
    conn.commit()
```

Why `execute_values` rather than `executemany` or SQLAlchemy Core
inserts:
- 10-50x faster on bulk loads (single round-trip per batch)
- Native `ON CONFLICT DO NOTHING` support via template substitution
- Small, well-understood API

Why not `COPY`: COPY is fastest for cold loads but doesn't natively
handle `ON CONFLICT DO NOTHING` (would need a staging table +
`INSERT ... SELECT ... ON CONFLICT`). For our row counts (< 50K per
table initially), `execute_values` is fast enough.

---

## Progress reporting

Per-table output:

```
=== nonprofits_seed ===
  source rows:      5000
  target before:       0
  inserted:         5000
  skipped (dup):       0
  target after:     5000
```

For `--dry-run`:

```
=== nonprofits_seed ===
  source rows:      5000
  target current:      0
  would insert:     5000  (delta: +5000)
```

At end of run, summary: total rows inserted across all tables, wall
time, exit status.

---

## Error handling

Per-row errors (e.g., one row violates a CHECK constraint) are logged
as `WARN` with the table, row hint (PK value), and exception class.
The remainder of the batch continues. `--apply` tracks total error
count; if >0, exit code is 3 (partial success) instead of 0.

Per-table errors (e.g., table missing on source, or dest table
unreachable) log `ERROR` and move to the next table. If any table
fails entirely, final exit code is 2.

Connection-level errors (e.g., RDS unreachable, IAM token expired)
abort with exit 2.

---

## Tests

**`lavandula/common/tests/unit/test_backfill_rds_0013.py`** — all tests
use an in-memory Postgres via `testing.postgresql` OR a live test DB
behind `LAVANDULA_LIVE_RDS=1`. For CI-friendly tests:

- Mock out the SQLAlchemy engine + raw psycopg2 connection
- Use a tempfile SQLite DB as source (real sqlite3)
- Assert SQL generated matches expected `INSERT ... ON CONFLICT ... DO NOTHING` shape
- Assert `execute_values` called with correct args

Test list:

| Name | Covers |
|------|--------|
| `test_argv_requires_exactly_one_of_dry_run_or_apply` | CLI |
| `test_argv_source_sqlite_required` | CLI |
| `test_column_intersection_skips_missing_sides` | alignment |
| `test_dry_run_counts_rows_no_writes` | dry-run path |
| `test_apply_inserts_rows_via_execute_values` | main insert path |
| `test_on_conflict_skips_existing_rows` | idempotency |
| `test_auto_id_tables_omit_id_column` | auto-id rule |
| `test_auto_id_skip_when_dest_has_rows` | safeguard |
| `test_per_row_error_continues_batch` | error resilience |
| `test_per_table_error_moves_to_next` | error resilience |
| `test_mismatched_table_logs_info` | alignment warning |
| `test_batch_size_honored` | perf |

Optional integration test behind `LAVANDULA_LIVE_RDS=1`:
- Creates a tempfile SQLite with 5 seeded rows
- Runs `--apply`
- Connects to `lava_prod1.lava_impact` as `app_user1` and verifies
  row count went from 0 → 5 (skipping if prior test data exists)
- Cleans up the 5 rows at the end

---

## Traps to Avoid

1. **Don't attempt to preserve source `id` values on auto-id tables.**
   Would require `ALTER SEQUENCE` calls and there's no benefit.

2. **Don't commit after each row.** Batch commits per-table keep the
   transaction count low. A per-table transaction also gives a clean
   rollback if the table fails mid-way.

3. **Don't use SQLAlchemy ORM for the insert.** `execute_values` is
   the performance tool. ORM inserts would be 10-50x slower.

4. **Don't use master credentials.** `app_user1` has INSERT on
   `lava_impact` per default privileges set by the Phase 0 migration.
   The backfill tool runs under the runtime IAM auth path.

5. **Don't skip the dry-run safety.** Real runs must be preceded by a
   `--dry-run` in the operator's workflow. The tool doesn't enforce
   this but the runbook (HANDOFF.md section) should.

6. **Don't log row contents.** Log PKs / row counts only. Some SQLite
   tables contain `first_page_text` or other user-content-derived
   data that shouldn't hit logs.

7. **Don't assume `PRAGMA table_info` returns columns in insert
   order.** Use explicit ORDER BY `cid` when iterating; then match
   against the target column list.

8. **Don't hard-code the tables list.** Build it from the known
   seven names but allow `--table` to narrow — useful for operator
   re-runs of a single table.

---

## Acceptance Criteria

**AC1** — `--dry-run` counts rows per table and compares to target;
writes zero rows.

**AC2** — `--apply` inserts rows via `execute_values` with
`ON CONFLICT DO NOTHING` for explicit-PK tables.

**AC3** — Re-running `--apply` against unchanged source produces
zero duplicates and exit 0.

**AC4** — Auto-id tables (`fetch_log`, `deletion_log`,
`budget_ledger`) skip the source `id` column and let Postgres assign
new values.

**AC5** — Re-running `--apply` on auto-id tables warns and skips
(unless `--apply-duplicates-ok`) — prevents accidental duplicate log
flood.

**AC6** — Column intersection: target columns missing in source are
filled with default/NULL; source columns missing in target are
logged INFO and ignored.

**AC7** — Per-row errors don't abort the batch; total error count is
surfaced in the final summary; exit code 3 if errors > 0.

**AC8** — Per-table errors move on to the next table; exit code 2
if any table fails.

**AC9** — Tool uses `make_app_engine()` for destination; no master
credentials read.

**AC10** — Unit tests mock all network/DB I/O; no AWS or real
Postgres calls in the default test suite.

**AC11** — `--table X` restricts backfill to one table; repeatable
flag allowed.

**AC12** — Row-content is never logged — only PKs and counts.

---

## Post-merge work (architect's job)

1. Run `--dry-run` against the current SQLite DBs to preview deltas:
   ```
   python -m lavandula.common.tools.backfill_rds \
       --source-sqlite /home/ubuntu/research/lavandula/nonprofits/data/seeds-eastcoast.db \
       --dry-run
   ```
2. If counts look right, run `--apply`.
3. Repeat for any other SQLite DBs of operational interest.
4. Update spec 0013 status note in `projectlist.md` to indicate Phase
   2 complete.
