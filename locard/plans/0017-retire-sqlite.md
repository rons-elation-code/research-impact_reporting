# Plan 0017 — Retire SQLite

**Spec**: `locard/specs/0017-retire-sqlite.md`  
**Date**: 2026-04-22

---

## Scope

One PR. Migrate ~10 Python modules from `sqlite3` to the SQLAlchemy
engine from `lavandula.common.db.make_app_engine()`. Delete the
Phase 3 dual-write infrastructure. Add migration `002_attribution_helper.sql`.
Adapt tests. 15 ACs from the spec.

---

## Build order (do it in this sequence to minimize broken-state window)

### Step 1 — Schema helper migration

New file `lavandula/migrations/rds/002_attribution_helper.sql`:

```sql
BEGIN;
SET search_path TO lava_impact, public;

CREATE OR REPLACE FUNCTION lava_impact.attribution_rank(attr TEXT)
RETURNS INTEGER
LANGUAGE SQL IMMUTABLE AS $$
  SELECT CASE attr
    WHEN 'own_domain' THEN 3
    WHEN 'platform_verified' THEN 2
    WHEN 'platform_unverified' THEN 1
    ELSE 0
  END
$$;

GRANT EXECUTE ON FUNCTION lava_impact.attribution_rank(TEXT)
  TO app_user1, ro_user1;

INSERT INTO schema_version (version, name)
  VALUES (2, 'attribution_rank_helper')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
```

Run it via psql as master. (Builder writes the file; the architect
applies it post-merge.)

### Step 2 — `lavandula/common/db.py` additions

Add to the adapter module:

```python
MIN_SCHEMA_VERSION = 2

def assert_schema_at_least(engine: Engine, min_version: int = MIN_SCHEMA_VERSION) -> None:
    """Fail fast if schema isn't current. Called by every production entrypoint."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            v = conn.execute(text(
                "SELECT COALESCE(MAX(version), 0) FROM lava_impact.schema_version"
            )).scalar()
    except Exception as exc:
        raise SystemExit(
            f"schema_version unreadable: {exc}. Apply "
            f"lavandula/migrations/rds/ migrations."
        ) from exc
    if v < min_version:
        raise SystemExit(
            f"schema at v{v}; code expects v{min_version}+. Apply newer migrations."
        )
```

### Step 3 — DELETE Phase 3 dual-write code

Remove files:
- `lavandula/reports/db_queue.py`
- `lavandula/reports/rds_db_writer.py`
- `lavandula/common/tools/verify_dual_write.py`

Remove tests:
- `lavandula/reports/tests/unit/test_rds_db_writer_0013p3.py`
- `lavandula/reports/tests/unit/test_db_writer_dual_0013p3.py`
- `lavandula/common/tests/unit/test_verify_dual_write_0013p3.py`
- `lavandula/reports/tests/unit/test_crawler_dual_write_flag_0013p3.py`
- Any `*_dual_*` or `*_rds_db_writer*` test files

Remove references:
- `LAVANDULA_DUAL_WRITE` env var handling in `crawler.py`
- `rds_writer` kwarg from all db_writer functions in
  `lavandula/reports/db_writer.py` and `lavandula/reports/budget.py`
- Any imports of deleted modules

### Step 4 — Migrate `lavandula/reports/db_writer.py`

Each function moves from raw `conn.execute()` (with `?` placeholders)
to SQLAlchemy `text()` with `:named` bind parameters. Pattern:

**Before**:
```python
def record_fetch(conn, *, ein, url_redacted, kind, ...):
    conn.execute(
        "INSERT INTO fetch_log (ein, url_redacted, kind, ...) VALUES (?, ?, ?, ...)",
        (ein, url_redacted, kind, ...)
    )
```

**After**:
```python
def record_fetch(engine, *, ein, url_redacted, kind, ...):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO lava_impact.fetch_log "
                 "(ein, url_redacted, kind, ...) "
                 "VALUES (:ein, :url, :kind, ...)"),
            {"ein": ein, "url": url_redacted, "kind": kind, ...}
        )
```

Key transformations per function:
- `record_fetch` — straight INSERT into `fetch_log`. Auto-id; omit `id`.
- `upsert_crawled_org` — INSERT ... ON CONFLICT (ein) DO UPDATE with
  `GREATEST(confirmed_report_count, EXCLUDED.confirmed_report_count)`
  (preserve prior count; the Phase 3 round-1 fix) and `last_crawled_at`
  always updated.
- `upsert_report` — INSERT ... ON CONFLICT (content_sha256) DO UPDATE
  with CASE expressions using `lava_impact.attribution_rank()`. See
  Step 5 for exact SQL shape.
- `record_deletion` — straight INSERT; auto-id.

All functions take an `engine` (not a `conn`), wrap their work in
`engine.begin()`, commit at block exit.

### Step 5 — `upsert_report` Postgres SQL

The full ON CONFLICT clause:

```sql
INSERT INTO lava_impact.reports (
  content_sha256, source_url_redacted, referring_page_url_redacted,
  redirect_chain_json, source_org_ein, discovered_via, hosting_platform,
  attribution_confidence, archived_at, content_type, file_size_bytes,
  page_count, first_page_text, pdf_creator, pdf_producer,
  pdf_creation_date, pdf_has_javascript, pdf_has_launch,
  pdf_has_embedded, pdf_has_uri_actions, classification,
  classification_confidence, classifier_model, classifier_version,
  classified_at, report_year, report_year_source, extractor_version
) VALUES (
  :sha, :url, :ref, :redirect, :ein, :disc, :platform,
  :attr, :archived, :ct, :size, :pages, :fpt, :creator,
  :producer, :cdate, :js, :launch, :embed, :uri,
  :class, :conf, :model, :cver, :cat, :year, :ysrc, :ext
)
ON CONFLICT (content_sha256) DO UPDATE SET
  -- Replace-when-stronger-attribution columns
  source_url_redacted = CASE
    WHEN lava_impact.attribution_rank(EXCLUDED.attribution_confidence)
       > lava_impact.attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.source_url_redacted
    ELSE lava_impact.reports.source_url_redacted
  END,
  referring_page_url_redacted = CASE
    WHEN lava_impact.attribution_rank(EXCLUDED.attribution_confidence)
       > lava_impact.attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.referring_page_url_redacted
    ELSE lava_impact.reports.referring_page_url_redacted
  END,
  source_org_ein = CASE
    WHEN lava_impact.attribution_rank(EXCLUDED.attribution_confidence)
       > lava_impact.attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.source_org_ein
    ELSE lava_impact.reports.source_org_ein
  END,
  attribution_confidence = CASE
    WHEN lava_impact.attribution_rank(EXCLUDED.attribution_confidence)
       > lava_impact.attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.attribution_confidence
    ELSE lava_impact.reports.attribution_confidence
  END,
  discovered_via = CASE
    WHEN lava_impact.attribution_rank(EXCLUDED.attribution_confidence)
       > lava_impact.attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.discovered_via
    ELSE lava_impact.reports.discovered_via
  END,
  hosting_platform = CASE
    WHEN lava_impact.attribution_rank(EXCLUDED.attribution_confidence)
       > lava_impact.attribution_rank(lava_impact.reports.attribution_confidence)
    THEN EXCLUDED.hosting_platform
    ELSE lava_impact.reports.hosting_platform
  END,
  -- Classification: prefer newer if new row has a classification
  classification = COALESCE(EXCLUDED.classification, lava_impact.reports.classification),
  classification_confidence = COALESCE(EXCLUDED.classification_confidence, lava_impact.reports.classification_confidence),
  classifier_model = COALESCE(EXCLUDED.classifier_model, lava_impact.reports.classifier_model),
  classifier_version = GREATEST(EXCLUDED.classifier_version, lava_impact.reports.classifier_version),
  classified_at = COALESCE(EXCLUDED.classified_at, lava_impact.reports.classified_at),
  -- Static fields: keep whichever we already had (first-write-wins for size/content_type)
  file_size_bytes = lava_impact.reports.file_size_bytes,
  content_type = lava_impact.reports.content_type,
  archived_at = lava_impact.reports.archived_at,
  -- PDF metadata: prefer existing if present, else new
  first_page_text = COALESCE(lava_impact.reports.first_page_text, EXCLUDED.first_page_text),
  pdf_creator = COALESCE(lava_impact.reports.pdf_creator, EXCLUDED.pdf_creator),
  pdf_producer = COALESCE(lava_impact.reports.pdf_producer, EXCLUDED.pdf_producer),
  page_count = COALESCE(lava_impact.reports.page_count, EXCLUDED.page_count),
  report_year = COALESCE(lava_impact.reports.report_year, EXCLUDED.report_year),
  report_year_source = COALESCE(lava_impact.reports.report_year_source, EXCLUDED.report_year_source),
  extractor_version = GREATEST(EXCLUDED.extractor_version, lava_impact.reports.extractor_version),
  -- active-content flags: OR (0|new), never downgrade a 1 to 0
  pdf_has_javascript  = GREATEST(EXCLUDED.pdf_has_javascript,  lava_impact.reports.pdf_has_javascript),
  pdf_has_launch      = GREATEST(EXCLUDED.pdf_has_launch,      lava_impact.reports.pdf_has_launch),
  pdf_has_embedded    = GREATEST(EXCLUDED.pdf_has_embedded,    lava_impact.reports.pdf_has_embedded),
  pdf_has_uri_actions = GREATEST(EXCLUDED.pdf_has_uri_actions, lava_impact.reports.pdf_has_uri_actions)
```

This replaces the SQLite read-then-write pattern with a single atomic
Postgres upsert. Semantic equivalent to the Phase 3 dual-write
closure but written once, correctly.

### Step 6 — Migrate `lavandula/reports/budget.py`

First: create `lavandula/common/lock_keys.py`:

```python
"""Central registry of Postgres advisory lock IDs.
Every caller of pg_advisory_xact_lock() must use a key from here.
Keys are arbitrary but must be unique project-wide."""

BUDGET_LEDGER_RESERVE = 0xB0DGE7
```

Then each of `check_and_reserve`, `settle`, `release`:
- Takes `engine` instead of `conn`
- Opens a single `engine.begin()` transaction
- First statement: `SELECT pg_advisory_xact_lock(:key)` where key
  comes from `lock_keys.BUDGET_LEDGER_RESERVE` (not a hardcoded
  literal)
- Then the read (COALESCE(SUM, 0)) and write
- Transaction commit releases the lock

### Step 7 — Migrate `lavandula/reports/crawler.py`

- Remove `--nonprofits-db` argparse arg entirely (AC per spec)
- Remove `--archive-dir` fallback to SQLite-local (S3 is the only
  archive now per 0007)
- At `run()` top: construct engine once; call
  `assert_schema_at_least(engine, MIN_SCHEMA_VERSION)`
- Pass `engine` (not `db_writer`) through `process_org`
- Remove `DBWriter` construction + lifecycle
- Worker threads call `db_writer.record_fetch(engine, ...)` etc.
  directly. Each call opens/commits its own transaction in the engine
  pool. Pool handles concurrency.

### Step 8 — Migrate remaining modules

Same pattern for:
- `lavandula/nonprofits/tools/seed_enumerate.py` — drop SCHEMA_SQL +
  _apply_migrations (schema lives in migration SQL now); rewrite
  `ensure_db()` to return an engine; rewrite insert logic with
  SQLAlchemy
- `lavandula/nonprofits/tools/resolve_websites.py` — engine + text()
- `lavandula/nonprofits/tools/batch_resolve.py` — URL source change
  (it already uses SQLAlchemy); point at `make_app_engine()`
- `lavandula/reports/tools/classify_null.py` — engine
- `lavandula/reports/tools/reconcile_s3.py` — engine
- `lavandula/reports/schema.py` — delete `SCHEMA_SQL`, `init_schema`,
  `ensure_db`; keep a thin `connect()` that returns
  `make_app_engine()` (for backward compat with imports, to be
  removed in a future sweep)

### Step 9 — Tests

Category A (MUST use real Postgres via testing.postgresql):
- `test_db_writer_upsert_report_merge_logic_0017.py` (NEW)
- `test_budget_advisory_lock_concurrent_0017.py` (NEW — AC15)
- `test_schema_version_check_0017.py` (NEW)
- `test_crawler_parallel_no_deadlock_0017.py` (NEW)

Category B (in-memory SQLite OK, via SQLAlchemy):
- Existing unit tests that don't exercise dialect-specific features
- Adapt fixtures from `sqlite3.connect(":memory:")` to
  `create_engine("sqlite:///:memory:")`

CI fixture: a `pytest` plugin that provides a `postgres_engine`
fixture backed by `testing.postgresql`, auto-applying
`001_initial_schema.sql` + `002_attribution_helper.sql` per test.

Add to `lavandula/common/tests/conftest.py`:

```python
import pytest
from pathlib import Path
try:
    import testing.postgresql
except ImportError:
    testing = None

@pytest.fixture
def postgres_engine():
    if testing is None:
        pytest.skip("testing.postgresql not installed")
    with testing.postgresql.Postgresql() as pg:
        from sqlalchemy import create_engine
        engine = create_engine(pg.url())
        _apply_migrations(engine)
        yield engine

def _apply_migrations(engine):
    from sqlalchemy import text
    migrations = sorted((Path(__file__).parents[3] / "migrations" / "rds").glob("*.sql"))
    with engine.connect() as conn:
        for m in migrations:
            conn.execute(text(m.read_text()))
        conn.commit()
```

### Step 10 — CI gates

Two grep gates + one Bandit gate. Grep is a fast first-pass; Bandit
is the authoritative check for SQL injection (per red-team finding).

```yaml
- name: Ensure no sqlite3 imports in production code
  run: |
    if grep -rn "^import sqlite3\|^from sqlite3" lavandula/ --include='*.py' \
       | grep -v '^lavandula/common/tools/backfill_rds\.py:' \
       | grep -v '/tests/'; then
      echo "FAIL: sqlite3 import in production code"; exit 1
    fi

- name: Ensure no hardcoded advisory lock keys
  run: |
    # pg_advisory_xact_lock callers must import the key from common.lock_keys
    if grep -rn 'pg_advisory_xact_lock' lavandula/ --include='*.py' \
       | grep -v '/tests/' \
       | grep -v 'lock_keys\.' ; then
      echo "FAIL: hardcoded advisory lock key"; exit 1
    fi

- name: Bandit S608 (SQL injection)
  run: |
    pip install bandit
    bandit -r lavandula/ \
      --exclude lavandula/common/tests,lavandula/nonprofits/tests,lavandula/reports/tests \
      --tests B608 \
      --severity-level low
```

Add to `lavandula/common/requirements-dev.in`: `bandit>=1.7`.

---

## Acceptance Criteria Checklist

All 15 ACs from spec:
- [ ] AC1 — grep gate passes (no sqlite3 in production outside backfill_rds)
- [ ] AC2 — db_queue.py deleted
- [ ] AC3 — rds_db_writer.py deleted
- [ ] AC4 — verify_dual_write.py deleted
- [ ] AC5 — LAVANDULA_DUAL_WRITE has no code path
- [ ] AC6 — schema.py stripped (no SCHEMA_SQL/init_schema/ensure_db)
- [ ] AC7 — seed_enumerate --target 15 writes to Postgres (manual, post-merge)
- [ ] AC8 — batch_resolve writes to Postgres (manual)
- [ ] AC9 — crawler writes to Postgres (manual)
- [ ] AC10 — classify_null writes to Postgres (manual)
- [ ] AC11 — reconcile_s3 works (manual)
- [ ] AC12 — backfill_rds still works
- [ ] AC13 — test suite green
- [ ] AC14 — 002 migration applied with attribution_rank function
- [ ] AC15 — budget concurrency test passes (Category A test)

Automated tests (builder's responsibility): AC1-AC6, AC12-AC15.
Manual tests (architect, post-merge): AC7-AC11.

---

## Traps (from spec)

Reiterated here for builder convenience — all enforced by the test
suite or CI gates.

1. Don't delete `backfill_rds.py` — grep gate explicitly excepts it
2. No f-string SQL — grep gate catches
3. `--nonprofits-db` removed entirely; no deprecation warning
4. 002 migration applied before any code merge that depends on `attribution_rank()`
5. TICK-002 HTTP/throttle preserved — only DB serialization goes
6. 15-org end-to-end is manual, post-merge
7. All write sites for a given table migrate together
