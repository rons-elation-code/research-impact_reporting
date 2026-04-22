# Spec 0017 — Retire SQLite, Use PostgreSQL Directly

**Status**: draft  
**Protocol**: SPIDER  
**Priority**: high (blocks resumption of production pipeline)  
**Date**: 2026-04-22  
**Supersedes**: Spec 0013 Phase 3 (dual-write) — retained in repo but flag off forever. Phase 4 (read flip) — cancelled, obsolete.  
**Depends on**: Spec 0013 Phases 0-2 (RDS provisioned, Python IAM adapter, backfill tool)

---

## Problem

The `LAVANDULA_DUAL_WRITE` infrastructure shipped in Spec 0013 Phase 3
is a code-coupled sync layer: every write path must thread an
`rds_writer` kwarg through its call chain for its writes to mirror to
RDS. This is architecturally wrong because:

1. **Sync is gated by code discipline** — a contract we hope
   contributors remember. Any new write path that forgets breaks sync
   silently.
2. **Phase 3 covered only crawler write paths** (reports / fetch_log /
   crawled_orgs / budget_ledger / deletion_log). Seed enumeration and
   resolver writes to `nonprofits_seed` / `runs` are SQLite-only,
   which means RDS's seed data goes stale from the moment dual-write
   is "on."
3. **Every new feature adds new sync surface to maintain.**

The original justifications for SQLite-primary (sub-millisecond local
writes, TICK-002 parallelism) don't hold at our scale. The crawler is
network-bound at >99% of wall time; the 2-4ms cross-AZ latency to RDS
per write adds ~0.1% to crawl duration. Postgres-only operation is
simpler, syncs-by-construction, and has room to grow.

---

## Goals

1. Remove SQLite from the runtime write path entirely.
2. Every module that currently calls `sqlite3.connect(...)` for
   production writes instead uses the SQLAlchemy engine from
   `lavandula/common/db.py` (Phase 1 adapter).
3. Delete the code-coupled dual-write infrastructure from Spec 0013
   Phase 3 — it's obsolete and confusing.
4. Preserve the backfill tool (`backfill_rds.py`) for importing
   historical SQLite data into the empty Postgres database.
5. Migrate tests to Postgres-compatible fixtures (in-memory SQLite
   may still be acceptable for unit tests IF the test surface is
   dialect-agnostic — addressed per-test below).

---

## Non-Goals

- **Removing SQLite as a dev-time option.** Developers without AWS
  access may still run tests against in-memory SQLite if we keep the
  SQLAlchemy engine abstraction dialect-agnostic. Production code
  itself doesn't branch on dialect.
- **Migrating away from SQLAlchemy.** SQLAlchemy stays as the DB
  abstraction. This spec removes the `sqlite3` module dependency,
  not SQLAlchemy.
- **Distributed multi-host writers.** That's a future capability
  (free to add later since there's one store now).
- **Archiving the historical SQLite DB files.** Those stay at their
  current paths under `/tmp/` and `lavandula/nonprofits/data/` as
  backfill sources. Archival policy is a separate ops decision.

---

## Design

### Module migration matrix

Every module that currently imports `sqlite3` or writes with raw
SQLite. Seven code-owning modules in scope:

| Module | Current state | After |
|--------|--------------|-------|
| `lavandula/nonprofits/tools/seed_enumerate.py` | `sqlite3.connect()` directly; inline SCHEMA_SQL; `_apply_migrations` for column additions | `make_app_engine()`; no SCHEMA_SQL (schema lives in `migrations/rds/*.sql`); `_apply_migrations` deleted (Postgres schema is current via Spec 0013 Phase 0 migration) |
| `lavandula/nonprofits/tools/resolve_websites.py` | `sqlite3.connect()` directly | `make_app_engine()` + `conn.execute(text(...))` |
| `lavandula/nonprofits/tools/batch_resolve.py` | Already uses SQLAlchemy but targets a SQLite URL | Targets Postgres engine from `make_app_engine()` |
| `lavandula/reports/crawler.py` | Via `schema.ensure_db(path)` → sqlite3 | Via `make_app_engine()`; `--nonprofits-db` flag deprecated |
| `lavandula/reports/db_writer.py` | Raw sqlite3 `conn.execute()` + Phase 3 dual-write closures | Single set of Postgres-flavored closures using `text()` parameterized SQL |
| `lavandula/reports/db_queue.py` | `DBWriter` queue for SQLite single-writer serialization | **DELETE** — Postgres handles multi-writer natively |
| `lavandula/reports/rds_db_writer.py` | Phase 3 dual-write RDSDBWriter | **DELETE** — obsolete |
| `lavandula/reports/schema.py` | `SCHEMA_SQL` (SQLite dialect) + `ensure_db`/`connect` helpers | `connect()` thin wrapper over `make_app_engine()`; `SCHEMA_SQL` and `init_schema` deleted; Postgres schema lives in `migrations/rds/*.sql` |
| `lavandula/reports/budget.py` | sqlite3 transactions for check_and_reserve / settle / release | SQLAlchemy engine; atomic via `SELECT ... FOR UPDATE` |
| `lavandula/reports/tools/classify_null.py` | Already uses sqlite3 internally | Migrate to engine |
| `lavandula/reports/tools/reconcile_s3.py` | sqlite3 | Engine |

### Schema source of truth

**`lavandula/migrations/rds/*.sql`** becomes the ONLY schema source.

Python `SCHEMA_SQL` constants in `schema.py` and
`seed_enumerate.py` get deleted. Anyone needing the schema reads the
SQL file.

New schema changes are versioned SQL files (`002_*.sql`,
`003_*.sql`, ...), applied via pgAdmin or psql. A `schema_version`
row is inserted at the end of each, which the Python adapter can
check at startup via:

```python
def assert_schema_at_least(engine, min_version: int) -> None:
    v = engine.execute(text(
        "SELECT MAX(version) FROM lava_impact.schema_version"
    )).scalar()
    if v < min_version:
        raise RuntimeError(f"Schema at v{v}; code expects v{min_version}+")
```

### Removing DBWriter queue

Postgres handles concurrent writes natively via its MVCC + row
locking. The TICK-002 `DBWriter` queue (single writer thread
serializing SQLite writes) was needed ONLY because SQLite serializes
at the DB-file level.

Deletion plan:
- `lavandula/reports/db_queue.py` — delete entirely
- `lavandula/reports/crawler.py` — remove `DBWriter` construction; worker threads acquire connections from the SQLAlchemy engine pool directly
- `lavandula/reports/db_writer.py` — remove `db_writer` kwarg from all functions; each function opens a connection via `engine.begin()` for its own transaction

**Connection pool sizing**: crawler uses 8 worker threads (TICK-002
default). Engine pool needs at least 10 connections (`pool_size=8`,
`max_overflow=4`). Default `make_app_engine()` already provides
`pool_size=5, max_overflow=10` — sufficient.

### Per-thread HTTP client stays

TICK-002's per-thread `ReportsHTTPClient` and singleton
`HostThrottle` are orthogonal to DB concerns and continue unchanged.

### Upsert semantics

Current SQLite patterns translate:

| SQLite | Postgres |
|--------|---------|
| `INSERT OR IGNORE INTO t` | `INSERT INTO t ... ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE INTO t` | `INSERT INTO t ... ON CONFLICT (pk) DO UPDATE SET ...` |
| `?` placeholders | `:name` (SQLAlchemy `text()`) |
| `AUTOINCREMENT id INTEGER PRIMARY KEY` | `BIGSERIAL PRIMARY KEY` + omit `id` in INSERT; use `RETURNING id` if the value is needed |
| `PRAGMA journal_mode=WAL` | N/A — Postgres has MVCC natively |

`upsert_report`'s attribution-rank merge logic (the "replace weaker
attribution with stronger") translates to a CASE-expression in the
ON CONFLICT UPDATE clause. Example:

```sql
INSERT INTO lava_impact.reports (content_sha256, attribution_confidence, source_url_redacted, ...)
VALUES (:sha, :attr, :url, ...)
ON CONFLICT (content_sha256) DO UPDATE SET
  attribution_confidence = CASE
    WHEN attribution_rank(EXCLUDED.attribution_confidence)
         > attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.attribution_confidence
    ELSE lava_impact.reports.attribution_confidence
  END,
  source_url_redacted = CASE
    WHEN attribution_rank(EXCLUDED.attribution_confidence)
         > attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.source_url_redacted
    ELSE lava_impact.reports.source_url_redacted
  END,
  ... (repeat for each replace-when-stronger column)
```

A small helper function `attribution_rank(TEXT) RETURNS INTEGER` gets
added to the schema via a new migration `002_attribution_helper.sql`.

### Budget ledger atomicity

`check_and_reserve` / `settle` / `release` need atomic reserve-then-
deduct semantics. SQLite relied on single-writer serialization. In
Postgres:

```sql
-- inside a single transaction:
SELECT SUM(cents_spent) FROM lava_impact.budget_ledger FOR UPDATE;
-- compute remaining budget
INSERT INTO lava_impact.budget_ledger (...) VALUES (...);
-- commit
```

The `FOR UPDATE` row-locking inside a transaction gives us the same
atomicity the SQLite single-writer pattern did, without requiring a
queue.

### Dual-write code deletion

Delete:
- `lavandula/reports/rds_db_writer.py`
- Any `rds_writer` kwargs in `db_writer.py`, `budget.py`
- `LAVANDULA_DUAL_WRITE` env var handling in `crawler.py`
- `verify_dual_write.py` from `lavandula/common/tools/`
- Phase 3 test files: `test_rds_db_writer_0013p3.py`, `test_db_writer_dual_0013p3.py`, `test_verify_dual_write_0013p3.py`

### Test migration

Unit tests currently use `sqlite3.connect(":memory:")` or tempfile
SQLite DBs. Two paths:

- **Path A (preferred for new tests)**: use `testing.postgresql` or a
  docker-compose-provided Postgres in CI. Local dev: developers run a
  local Postgres container.
- **Path B (pragmatic)**: keep SQLAlchemy engine tests dialect-
  agnostic where the test surface doesn't exercise dialect-specific
  features. Those tests use in-memory SQLite as a test-only optimization
  and still pass on Postgres in CI.

For this spec, most tests migrate to Path B to minimize disruption. A
future spec can move to Path A if/when we have a CI Postgres fixture.

---

## Acceptance Criteria

**AC1** — No production module imports `sqlite3` directly. Verified by
grep gate in CI: `grep -rn "^import sqlite3\|^from sqlite3" lavandula/
--include='*.py' | grep -v tests/ | grep -v backfill_rds.py` returns
zero lines.

**AC2** — `lavandula/reports/db_queue.py` is deleted.

**AC3** — `lavandula/reports/rds_db_writer.py` is deleted.

**AC4** — `lavandula/common/tools/verify_dual_write.py` is deleted.

**AC5** — `LAVANDULA_DUAL_WRITE` env var has no effect on any module
(the code path is gone).

**AC6** — `lavandula/reports/schema.py` no longer contains `SCHEMA_SQL`,
`init_schema`, or `ensure_db`. A thin `connect(db_path=None)` helper
that delegates to `make_app_engine()` remains (for backwards-compat
with existing imports; can be deleted in a later sweep).

**AC7** — `seed_enumerate` successfully pulls a small target count
(e.g., `--target 15`) against a filter, writing directly to Postgres.
Row count in `lava_impact.nonprofits_seed` matches target.

**AC8** — `batch_resolve` runs against the 15 fresh rows, updating
`resolver_status` in Postgres.

**AC9** — `crawler` runs against resolved subset, writing reports +
fetch_log + crawled_orgs directly to Postgres.

**AC10** — `classify_null` runs against the reports, writing
classifications + budget_ledger directly to Postgres.

**AC11** — `reconcile_s3.py` works against Postgres as the reports
store.

**AC12** — `backfill_rds.py` still works (unchanged) — needed for
post-migration restore from SQLite.

**AC13** — Existing test suite runs green. Tests that previously used
in-memory sqlite3 directly are migrated to either SQLAlchemy-engine
fixtures or testing.postgresql. A small number of tests may gain
SQLAlchemy-engine-in-memory-sqlite as a compatibility shim (allowed
per "Path B" above).

**AC14** — `attribution_rank()` helper function exists in
`lava_impact` schema, added via new migration `002_attribution_helper.sql`.

**AC15** — Budget reserve/settle/release are atomic under concurrent
callers. Test: spawn 4 threads each calling check_and_reserve with
budget that would overflow if serialized incorrectly; assert no
overflow.

---

## Traps to Avoid

1. **Don't delete `backfill_rds.py`** — we need it for the post-migration
   restore from archival SQLite files.

2. **Don't leave zombie `sqlite3` imports** — even in module-level try/
   except blocks. The grep gate catches them.

3. **Don't leave the `--nonprofits-db` CLI flag with a SQLite path
   default.** The crawler and other tools should accept no DB arg (or
   only `--engine-url` for override) and default to the engine from
   SSM config.

4. **Don't migrate the schema without a version bump.** The new
   `attribution_rank` helper is migration `002`. The schema_version
   row must be inserted.

5. **Don't break TICK-002 parallelism.** The HTTP-client-per-thread
   and HostThrottle singleton stay as-is. Only the DB serialization
   layer (DBWriter queue) goes away.

6. **Don't skip the 15-org end-to-end test.** It's the acceptance gate.
   If any phase of the pipeline breaks against Postgres, we find out at
   scale 15, not 5000.

7. **Don't do partial migration.** All write sites for each table
   must move together. Mixed code paths (some to Postgres, some to
   SQLite) would recreate the sync problem this spec is solving.

---

## Security Considerations

No new secrets. No new auth flows. Uses existing Phase 1 IAM adapter,
which is already hardened. KMS / SSL / least-privilege roles
unchanged.

Threat model stays the same as Phase 1.

---

## Post-merge sequence (architect, not builder)

1. Run the 15-org end-to-end test (AC7-AC10). If anything fails,
   file follow-up fixes.
2. Assuming green: DELETE all rows from the 7 `lava_impact` tables
   (already empty from pre-migration truncate, but belt-and-
   suspenders).
3. Run `backfill_rds --apply` against the three archival SQLite
   sources:
   - `/home/ubuntu/research/lavandula/nonprofits/data/seeds-eastcoast.db`
   - `/tmp/tx-test/seeds.db`
   - `/tmp/tx-test/haiku-crawl-tick002/data/reports.db`
4. Verify row counts match pre-migration baseline (5100 seeds, 160
   reports, 88 crawled_orgs, 2257 fetch_log, 145 budget_ledger, 2
   runs).
5. Resume normal operations.

---

## Files Changed

| Path | Status |
|------|--------|
| `lavandula/nonprofits/tools/seed_enumerate.py` | Migrate to engine |
| `lavandula/nonprofits/tools/resolve_websites.py` | Migrate |
| `lavandula/nonprofits/tools/batch_resolve.py` | Engine URL source change |
| `lavandula/reports/crawler.py` | Migrate + drop DBWriter |
| `lavandula/reports/db_writer.py` | Migrate + drop dual-write kwargs |
| `lavandula/reports/db_queue.py` | DELETE |
| `lavandula/reports/rds_db_writer.py` | DELETE |
| `lavandula/reports/schema.py` | Strip SCHEMA_SQL; keep thin connect helper |
| `lavandula/reports/budget.py` | Migrate |
| `lavandula/reports/tools/classify_null.py` | Migrate |
| `lavandula/reports/tools/reconcile_s3.py` | Migrate |
| `lavandula/common/tools/verify_dual_write.py` | DELETE |
| `lavandula/migrations/rds/002_attribution_helper.sql` | NEW |
| Tests across both packages | Migrate fixtures |
