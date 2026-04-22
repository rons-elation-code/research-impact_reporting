# Plan 0013 Phase 3 — SQLite/RDS Dual-Write Wrapper

**Spec**: `locard/specs/0013-rds-postgres-migration.md` Phase 3  
**Depends on**: Phase 1 (IAM adapter, merged) + Phase 2 (backfill tool, merged)  
**Date**: 2026-04-22

---

## Scope

Enable the crawler to write every `reports` / `fetch_log` /
`crawled_orgs` / `budget_ledger` / `deletion_log` update to BOTH
SQLite (unchanged) AND RDS (new). SQLite stays authoritative;
RDS is best-effort during stabilization.

Feature flag: `LAVANDULA_DUAL_WRITE` (default off). When off,
behavior is byte-identical to pre-0013.

Phase 4 (read flip) is NOT in this PR.

---

## Non-goals

- Changing crawler CLI / argv — the feature flag is purely env-var
- Unifying SQLite + Postgres via a single SQLAlchemy dialect layer —
  deferred to Phase 5+ retire
- Modifying the seed-enumerate or resolver write paths — out of scope
  (those touch `nonprofits_seed` / `runs`, backfill handles them for
  now; crawl hot-path dual-write is the near-term need)
- `lavandula.nonprofits.*` hot-path changes — scope is crawler only

---

## Architecture

### The wrapper principle

Each existing `db_writer` function already accepts an optional
`db_writer` kwarg (the TICK-002 SQLite queue). This PR adds a
parallel optional `rds_writer` kwarg. When BOTH are provided, each
function submits TWO parallel closures — one SQLite-flavored, one
Postgres-flavored — to separate queues.

```python
def upsert_report(conn, *, db_writer=None, rds_writer=None, ...):
    def _do_sqlite(target_conn):
        # existing logic unchanged
        ...
    def _do_rds(pg_conn):
        # parallel Postgres logic with Postgres-flavored SQL
        ...
    # Dispatch to SQLite (mandatory) and RDS (optional, best-effort)
    if db_writer is not None:
        db_writer.put(_do_sqlite)
    else:
        _do_sqlite(conn); conn.commit()
    if rds_writer is not None:
        rds_writer.put(_do_rds)
```

### RDS writer — new class

`lavandula/reports/rds_db_writer.py`:

```python
class RDSDBWriter:
    """Single-thread async writer that mirrors DBWriter's interface,
    but writes to RDS via SQLAlchemy. Queue is bounded; failures are
    logged and dropped (RDS is best-effort during Phase 3)."""

    def __init__(self, engine, maxsize: int = 256): ...
    def start(self) -> None: ...
    def put(self, op: Callable[[psycopg2_conn], None], timeout: float = 30.0) -> None:
        """Enqueue a Postgres-flavored write closure. Non-blocking
        unless queue is full; if queue is saturated, logs a WARNING
        and drops the op (RDS is best-effort). Never raises."""
    def stop(self) -> None:
        """Drain queue with a timeout; log drift if ops remain."""
    def is_alive(self) -> bool: ...
```

**Key difference from `DBWriter` (SQLite)**:
- `DBWriter` raises on failure (SQLite is authoritative)
- `RDSDBWriter` logs and drops on failure (RDS is best-effort)
- `RDSDBWriter` uses SQLAlchemy engine connections (via `engine.raw_connection()` for psycopg2-level access, since we're using `execute_values`-style bulk ops in some paths)

### Per-function dual closure

Five db_writer functions grow an `rds_writer` kwarg + parallel
closure:

1. `record_fetch` — `fetch_log` INSERT
2. `upsert_crawled_org` — `crawled_orgs` INSERT ... ON CONFLICT DO
   UPDATE
3. `upsert_report` — `reports` INSERT ... ON CONFLICT DO UPDATE with
   attribution-rank logic
4. `record_deletion` — `deletion_log` INSERT
5. Budget ledger writes (in `budget.py` — 2 call sites wrapping
   `INSERT INTO budget_ledger`)

For each, the Postgres closure uses:
- Placeholders: `%s` (psycopg2 style) instead of `?` (sqlite3 style)
- Schema qualification: all inserts target `lava_impact.<table>`
- Conflict clauses: `ON CONFLICT (pk) DO UPDATE SET col = EXCLUDED.col, ...`
  instead of SQLite's `INSERT OR REPLACE`
- Auto-id tables (`fetch_log`, `budget_ledger`, `deletion_log`): omit
  the `id` column entirely, let Postgres assign

### Crawler wiring

`crawler.run()`:

```python
dual_write_enabled = os.getenv("LAVANDULA_DUAL_WRITE", "").lower() in ("1", "true", "yes")
rds_writer = None
if dual_write_enabled:
    from lavandula.common.db import make_app_engine
    engine = make_app_engine()
    rds_writer = RDSDBWriter(engine)
    rds_writer.start()

# ... existing DBWriter setup ...

try:
    # ... per-org processing, passing BOTH writers to each db_writer call ...
finally:
    if rds_writer is not None:
        rds_writer.stop()  # drain RDS queue with timeout
    writer.stop()  # existing SQLite writer
```

### Failure model

| Scenario | Behavior |
|----------|---------|
| RDS reachable, write succeeds | Both backends have the row |
| RDS write raises (5xx, permission, constraint violation) | WARN log, row skipped on RDS side, SQLite unchanged, crawler continues |
| RDS unreachable at startup | Startup `make_app_engine()` succeeds (lazy), first PUT fails — treated as scenario above |
| RDS queue saturated (>256 backlog) | WARN log, op dropped, crawler continues |
| RDSDBWriter thread dies | WARN log each subsequent PUT ("writer not alive"), ops dropped, crawler continues |
| IAM token expired mid-run | SQLAlchemy's `pool_pre_ping` + `pool_recycle` from Phase 1 handles this; transparent retry within a single PUT |

Critical property: **no RDS failure mode can fail the SQLite write
or interrupt the crawler**. The only way RDS failures surface
operationally is via the `verify_dual_write` tool and the WARN logs.

### Drift detection tool

`lavandula/common/tools/verify_dual_write.py`:

```
python -m lavandula.common.tools.verify_dual_write \
  --sqlite PATH/reports.db \
  [--table TABLE]...
```

For each table, compares:
- Row counts
- MIN/MAX of timestamp columns (catches "RDS is 30 min behind")
- A sample of PKs present in one backend but not the other

Outputs:
```
=== reports ===
  sqlite count:    180
  rds count:       178   (drift: -2)
  missing in rds:  <sha1>, <sha2>
```

Exit 0 if drift is zero; exit 1 if any drift detected (lets ops
decide to backfill or investigate).

---

## Deliverables

| Path | Status |
|------|--------|
| `lavandula/reports/rds_db_writer.py` | NEW |
| `lavandula/reports/db_writer.py` | EXTEND — five functions grow `rds_writer` kwarg + parallel closure |
| `lavandula/reports/budget.py` | EXTEND — two writes get `rds_writer` parallel closure |
| `lavandula/reports/crawler.py` | EXTEND — feature flag → construct/start/stop RDSDBWriter |
| `lavandula/common/tools/verify_dual_write.py` | NEW |
| `lavandula/reports/tests/unit/test_rds_db_writer_0013p3.py` | NEW — writer lifecycle + failure model |
| `lavandula/reports/tests/unit/test_db_writer_dual_0013p3.py` | NEW — each of 5 functions in dual-write mode |
| `lavandula/common/tests/unit/test_verify_dual_write_0013p3.py` | NEW |

---

## Acceptance Criteria

**AC1** — `LAVANDULA_DUAL_WRITE=0` (or unset): crawler behavior is
byte-identical to pre-0013. No RDS connection opened. Verified by
a test that spies on `make_app_engine` and asserts zero calls.

**AC2** — `LAVANDULA_DUAL_WRITE=1`: crawler opens an RDS engine at
startup via `make_app_engine()` and constructs an `RDSDBWriter`.

**AC3** — Each of the 5 db_writer functions, when called with both
`db_writer` and `rds_writer`, submits a parallel Postgres-flavored
closure to the RDS queue.

**AC4** — RDS write failure (simulated) is logged as WARN, does NOT
raise, does NOT affect the SQLite write path.

**AC5** — `RDSDBWriter.put()` never blocks the caller beyond a
30-second queue-put timeout. On queue saturation, the op is dropped
with WARN.

**AC6** — `RDSDBWriter.stop()` drains the queue with a configurable
timeout (default 30s), logs drift if ops remain.

**AC7** — `RDSDBWriter` thread death is detected by `is_alive()`; the
crawler's `finally` block handles it gracefully (no crash at stop).

**AC8** — `verify_dual_write.py` reports row-count drift per table
and exit 0/1 correctly.

**AC9** — Postgres write SQL uses `%s` placeholders, schema-
qualified table names (`lava_impact.<table>`), and correct
`ON CONFLICT` clauses per table.

**AC10** — Auto-id tables (`fetch_log`, `budget_ledger`,
`deletion_log`) omit the `id` column in the RDS INSERT.

**AC11** — Unit tests mock all RDS I/O; no live AWS in the default
suite. Integration test behind `LAVANDULA_LIVE_RDS=1` verifies an
actual round-trip insert for one row per table.

**AC12** — Per-function SQL tests assert the generated Postgres
statement matches the expected `INSERT INTO lava_impact.X ...`
shape with correct placeholders and conflict clauses.

---

## Traps to Avoid

1. **Don't mutate the shared DBWriter signature.** Existing callers
   pass `db_writer=writer`; the new `rds_writer` is optional and
   defaulted to `None`. Back-compat is preserved.

2. **Don't use the same queue for SQLite and RDS.** They have
   different failure semantics (SQLite must succeed; RDS is
   best-effort). Keep them separate.

3. **Don't let RDS failures surface as crawler exit codes.** The
   only acceptable observable is a WARN log. A poisoned RDS path
   must not degrade the authoritative SQLite path.

4. **Don't forget `pool_pre_ping` is on the engine already** (Phase
   1). Mid-connection token expiry is handled transparently. Don't
   add duplicate retry logic.

5. **Don't pass the SQLAlchemy ORM session.** The closures receive
   a raw psycopg2 connection via `engine.raw_connection()`. This
   mirrors the TICK-002 pattern where SQLite closures receive a
   `sqlite3.Connection`.

6. **Don't skip the `id`-column omission for auto-id tables.** SQLite
   has `id` as the source of truth (autoincrement); Postgres MUST
   assign its own to avoid PK conflicts. Same rule as Phase 2.

7. **Don't duplicate the queue-saturation abort logic from DBWriter.**
   SQLite queue saturation = crawler failure (TICK-002 round 5). RDS
   queue saturation = drop with WARN. The two writers' failure
   models differ on purpose.

8. **Don't ship without the drift detector.** `verify_dual_write` is
   how we know dual-write is stable enough to flip reads in Phase 4.
   Without it, we're flying blind.

---

## Post-merge work (architect, not builder)

1. Set `LAVANDULA_DUAL_WRITE=1` in a small test crawl (e.g., re-run
   88-org TX against the Haiku seeds) and verify RDS receives all
   rows.
2. Run `verify_dual_write --sqlite <reports.db>` to confirm zero drift.
3. Let dual-write run for 2+ real crawls; check drift periodically.
4. When drift stays at zero across ≥2 crawls, move to Phase 4 (read flip).
