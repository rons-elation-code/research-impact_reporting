"""One-time SQLite → RDS Postgres backfill (Spec 0013 Phase 2).

Copies rows from a source SQLite DB file into the configured RDS
Postgres instance (schema `lava_impact`) using fast batched inserts.
Runs under the runtime IAM-auth path via
`lavandula.common.db.make_app_engine` — no master credentials are
ever read.

Seven tables are known. Four have explicit primary keys and use
`ON CONFLICT (pk) DO NOTHING` so re-runs are idempotent. Three are
auto-increment log tables; the source `id` column is dropped and
Postgres assigns fresh sequence values. A safeguard prevents
accidental duplicate log floods on re-run: when the destination
already has rows in an auto-id table, the table is skipped unless
`--apply-duplicates-ok` is passed.

Usage
-----
    python -m lavandula.common.tools.backfill_rds \\
        --source-sqlite /path/to/seeds.db \\
        [--table TABLE]... \\
        [--batch-size 1000] \\
        [--schema lava_impact] \\
        (--dry-run | --apply) \\
        [--apply-duplicates-ok]

Exit codes
----------
  0  success, no errors
  2  connection-level or per-table failure
  3  partial success (>=1 per-row error)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

log = logging.getLogger("lavandula.common.tools.backfill_rds")


def _default_execute_values():
    """Lazy import so unit tests never need psycopg2 installed."""
    from psycopg2.extras import execute_values  # type: ignore
    return execute_values


# ---------------------------------------------------------------------------
# Table registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TableSpec:
    name: str
    pk: str | None  # None for auto-id tables
    auto_id: bool = False


TABLES: tuple[TableSpec, ...] = (
    TableSpec("nonprofits_seed", pk="ein"),
    TableSpec("reports",         pk="content_sha256"),
    TableSpec("crawled_orgs",    pk="ein"),
    TableSpec("runs",            pk="run_id"),
    TableSpec("fetch_log",       pk=None, auto_id=True),
    TableSpec("deletion_log",    pk=None, auto_id=True),
    TableSpec("budget_ledger",   pk=None, auto_id=True),
)

TABLE_BY_NAME: dict[str, TableSpec] = {t.name: t for t in TABLES}


# ---------------------------------------------------------------------------
# Identifier validation — we build some SQL with f-strings since psycopg2
# can't parameterize table / column / schema names. Strict whitelist.
# ---------------------------------------------------------------------------

import re
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Column discovery
# ---------------------------------------------------------------------------

def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for `table` in insert order (PRAGMA cid ORDER)."""
    cur = conn.execute(f"PRAGMA table_info({_safe_ident(table)})")
    rows = cur.fetchall()
    # PRAGMA returns (cid, name, type, notnull, dflt_value, pk). Sort by cid.
    rows = sorted(rows, key=lambda r: r[0])
    return [r[1] for r in rows]


def _postgres_columns(pg_cur, schema: str, table: str) -> list[str]:
    pg_cur.execute(
        "SELECT column_name "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (schema, table),
    )
    return [r[0] for r in pg_cur.fetchall()]


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.execute(f"SELECT COUNT(*) FROM {_safe_ident(table)}")
    return int(cur.fetchone()[0])


def _postgres_count(pg_cur, schema: str, table: str) -> int:
    pg_cur.execute(
        f'SELECT COUNT(*) FROM "{_safe_ident(schema)}"."{_safe_ident(table)}"'
    )
    return int(pg_cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Batch insert
# ---------------------------------------------------------------------------

def _build_insert_sql(
    schema: str, table: str, cols: Sequence[str], pk: str | None
) -> str:
    schema = _safe_ident(schema)
    table = _safe_ident(table)
    col_list = ", ".join(f'"{_safe_ident(c)}"' for c in cols)
    sql = (
        f'INSERT INTO "{schema}"."{table}" ({col_list}) '
        "VALUES %s"
    )
    if pk is not None:
        sql += f' ON CONFLICT ("{_safe_ident(pk)}") DO NOTHING'
    return sql


def _chunked(rows: Iterable[tuple], size: int):
    batch: list[tuple] = []
    for r in rows:
        batch.append(r)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


@dataclass
class TableResult:
    table: str
    source_rows: int = 0
    target_before: int = 0
    target_after: int = 0
    inserted: int = 0
    skipped_existing: bool = False
    per_row_errors: int = 0
    table_error: str | None = None


# ---------------------------------------------------------------------------
# Per-table backfill
# ---------------------------------------------------------------------------

def backfill_table(
    *,
    spec: TableSpec,
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    batch_size: int,
    dry_run: bool,
    apply_duplicates_ok: bool,
    execute_values,  # injected: psycopg2.extras.execute_values
) -> TableResult:
    """Backfill a single table. Returns a TableResult.

    Mutates `pg_conn` by issuing BEGIN / COMMIT for --apply mode.
    On per-table failure the Postgres transaction is rolled back.
    """
    res = TableResult(table=spec.name)

    try:
        src_cols = _sqlite_columns(sqlite_conn, spec.name)
    except sqlite3.Error as exc:
        res.table_error = f"sqlite PRAGMA failed: {exc.__class__.__name__}"
        log.error("%s: source unavailable: %s", spec.name, res.table_error)
        return res

    if not src_cols:
        res.table_error = "source table missing or empty schema"
        log.error("%s: %s", spec.name, res.table_error)
        return res

    with pg_conn.cursor() as cur:
        try:
            dst_cols = _postgres_columns(cur, schema, spec.name)
        except Exception as exc:  # noqa: BLE001
            pg_conn.rollback()
            res.table_error = (
                f"postgres column lookup failed: {exc.__class__.__name__}"
            )
            log.error("%s: %s", spec.name, res.table_error)
            return res

    if not dst_cols:
        res.table_error = "destination table not found in schema"
        log.error("%s: %s in schema %s", spec.name, res.table_error, schema)
        return res

    # Alignment: intersection preserves source insert order.
    src_set = set(src_cols)
    dst_set = set(dst_cols)

    only_src = [c for c in src_cols if c not in dst_set]
    only_dst = [c for c in dst_cols if c not in src_set]
    if only_src:
        log.info("%s: source-only columns ignored: %s",
                 spec.name, ", ".join(only_src))
    if only_dst:
        log.info("%s: target-only columns left to default/NULL: %s",
                 spec.name, ", ".join(only_dst))

    cols = [c for c in src_cols if c in dst_set]

    # For auto-id tables, drop the id column even if present on both sides.
    if spec.auto_id:
        cols = [c for c in cols if c != "id"]

    if not cols:
        res.table_error = "no columns overlap between source and target"
        log.error("%s: %s", spec.name, res.table_error)
        return res

    # Counts (source + target-before)
    try:
        res.source_rows = _sqlite_count(sqlite_conn, spec.name)
    except sqlite3.Error as exc:
        res.table_error = f"sqlite count failed: {exc.__class__.__name__}"
        log.error("%s: %s", spec.name, res.table_error)
        return res

    with pg_conn.cursor() as cur:
        try:
            res.target_before = _postgres_count(cur, schema, spec.name)
        except Exception as exc:  # noqa: BLE001
            pg_conn.rollback()
            res.table_error = (
                f"postgres count failed: {exc.__class__.__name__}"
            )
            log.error("%s: %s", spec.name, res.table_error)
            return res

    # Auto-id safeguard: skip if dest has rows, unless override.
    if spec.auto_id and res.target_before > 0 and not apply_duplicates_ok:
        log.warning(
            "%s: auto-id table already has %d rows; skipping "
            "(pass --apply-duplicates-ok to force)",
            spec.name, res.target_before,
        )
        res.skipped_existing = True
        res.target_after = res.target_before
        return res

    if dry_run:
        would = max(0, res.source_rows - res.target_before)
        log.info(
            "%s: dry-run source=%d target=%d would_insert=%d",
            spec.name, res.source_rows, res.target_before, would,
        )
        res.target_after = res.target_before
        res.inserted = would  # reported as "would insert"
        return res

    # --apply path.
    sql = _build_insert_sql(schema, spec.name, cols, spec.pk)

    quoted_cols = ", ".join(f'"{_safe_ident(c)}"' for c in cols)
    select_sql = (
        f"SELECT {quoted_cols} FROM {_safe_ident(spec.name)}"
    )
    src_cursor = sqlite_conn.execute(select_sql)

    total_inserted = 0
    per_row_errors = 0
    sp_batch = "bf_batch"
    sp_row = "bf_row"

    def _released_rowcount(cur) -> int:
        # Under `ON CONFLICT DO NOTHING`, Postgres reports the number of
        # actually-inserted rows in cur.rowcount. `execute_values` emits
        # a single INSERT per call so rowcount is authoritative.
        rc = getattr(cur, "rowcount", 0) or 0
        return rc if rc > 0 else 0

    try:
        for batch in _chunked(iter(src_cursor.fetchone, None), batch_size):
            # Per-batch savepoint: a failing batch unwinds only itself,
            # never previously-committed batches for this table.
            try:
                with pg_conn.cursor() as cur:
                    cur.execute(f"SAVEPOINT {sp_batch}")
                    execute_values(cur, sql, batch, page_size=batch_size)
                    inserted_in_batch = _released_rowcount(cur)
                    cur.execute(f"RELEASE SAVEPOINT {sp_batch}")
                total_inserted += inserted_in_batch
            except Exception as batch_exc:  # noqa: BLE001
                # Undo the failing batch only; prior batches remain.
                with pg_conn.cursor() as cur:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp_batch}")
                log.warning(
                    "%s: batch of %d failed (%s); falling back row-by-row",
                    spec.name, len(batch), batch_exc.__class__.__name__,
                )
                for row in batch:
                    try:
                        with pg_conn.cursor() as cur:
                            cur.execute(f"SAVEPOINT {sp_row}")
                            execute_values(cur, sql, [row], page_size=1)
                            inserted_in_row = _released_rowcount(cur)
                            cur.execute(f"RELEASE SAVEPOINT {sp_row}")
                        total_inserted += inserted_in_row
                    except Exception as row_exc:  # noqa: BLE001
                        with pg_conn.cursor() as cur:
                            cur.execute(f"ROLLBACK TO SAVEPOINT {sp_row}")
                        per_row_errors += 1
                        # Log PK only; never log row contents.
                        pk_hint = _row_pk_hint(cols, row, spec.pk)
                        log.warning(
                            "%s: row error pk=%s (%s)",
                            spec.name, pk_hint, row_exc.__class__.__name__,
                        )
        pg_conn.commit()
    except Exception as exc:  # noqa: BLE001
        pg_conn.rollback()
        res.table_error = (
            f"insert path failed: {exc.__class__.__name__}"
        )
        log.error("%s: %s", spec.name, res.table_error)
        return res

    res.inserted = total_inserted
    res.per_row_errors = per_row_errors

    with pg_conn.cursor() as cur:
        try:
            res.target_after = _postgres_count(cur, schema, spec.name)
        except Exception:  # noqa: BLE001
            pg_conn.rollback()
            res.target_after = res.target_before + total_inserted

    return res


def _row_pk_hint(cols: Sequence[str], row: Sequence[Any], pk: str | None) -> str:
    if pk is None or pk not in cols:
        return "<auto-id>"
    try:
        return str(row[cols.index(pk)])
    except Exception:  # noqa: BLE001
        return "<unknown>"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m lavandula.common.tools.backfill_rds",
        description="Backfill SQLite rows into RDS Postgres (spec 0013).",
    )
    ap.add_argument(
        "--source-sqlite", required=True,
        help="Path to source SQLite DB file",
    )
    ap.add_argument(
        "--table", action="append", default=[],
        help="Specific table(s) to backfill; repeatable. "
             "Default: all seven known tables.",
    )
    ap.add_argument(
        "--batch-size", type=int, default=1000,
        help="Rows per execute_values call (default 1000).",
    )
    ap.add_argument(
        "--schema", default="lava_impact",
        help="Target Postgres schema (default lava_impact).",
    )
    ap.add_argument(
        "--apply-duplicates-ok", action="store_true",
        help="Allow re-inserting auto-id tables that already have rows.",
    )

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Count rows; write nothing.")
    mode.add_argument("--apply", action="store_true",
                      help="Perform the inserts.")

    return ap.parse_args(argv)


def _resolve_table_specs(requested: Sequence[str]) -> list[TableSpec]:
    if not requested:
        return list(TABLES)
    specs: list[TableSpec] = []
    for name in requested:
        if name not in TABLE_BY_NAME:
            raise SystemExit(
                f"unknown table {name!r}; known: {sorted(TABLE_BY_NAME)}"
            )
        specs.append(TABLE_BY_NAME[name])
    return specs


def _print_result(res: TableResult, dry_run: bool) -> None:
    print(f"=== {res.table} ===")
    if res.table_error:
        print(f"  ERROR: {res.table_error}")
        return
    if res.skipped_existing:
        print(f"  source rows:      {res.source_rows}")
        print(f"  target rows:      {res.target_before}")
        print(f"  SKIPPED (auto-id dest non-empty; pass "
              f"--apply-duplicates-ok to force)")
        return
    if dry_run:
        print(f"  source rows:      {res.source_rows}")
        print(f"  target current:   {res.target_before}")
        print(f"  would insert:     {res.inserted}  "
              f"(delta: {res.inserted:+d})")
        return
    print(f"  source rows:      {res.source_rows}")
    print(f"  target before:    {res.target_before}")
    print(f"  inserted:         {res.inserted}")
    print(f"  target after:     {res.target_after}")
    if res.per_row_errors:
        print(f"  per-row errors:   {res.per_row_errors}")


def run(
    *,
    source_sqlite: str,
    tables: Sequence[str],
    batch_size: int,
    schema: str,
    dry_run: bool,
    apply_duplicates_ok: bool,
    engine_factory=None,
    execute_values=None,
) -> int:
    """Library entry point. Returns the process exit code.

    `engine_factory` and `execute_values` are injection points for tests.
    In production they default to `make_app_engine` and
    `psycopg2.extras.execute_values`.
    """
    if engine_factory is None:
        from lavandula.common.db import make_app_engine
        engine_factory = make_app_engine
    if execute_values is None:
        execute_values = _default_execute_values()

    specs = _resolve_table_specs(tables)
    _safe_ident(schema)  # fail fast on bad schema name

    try:
        sqlite_conn = sqlite3.connect(source_sqlite)
    except sqlite3.Error as exc:
        log.error("cannot open source sqlite %s: %s", source_sqlite, exc)
        return 2

    try:
        engine = engine_factory()
    except Exception as exc:  # noqa: BLE001
        log.error("cannot build RDS engine: %s", exc.__class__.__name__)
        sqlite_conn.close()
        return 2

    results: list[TableResult] = []
    started = time.monotonic()
    table_failure = False

    try:
        with engine.connect() as sa_conn:
            pg_conn = sa_conn.connection  # raw psycopg2 connection
            # Turn off SQLAlchemy-managed BEGIN; we manage transactions
            # per-table via commit/rollback.
            try:
                pg_conn.autocommit = False
            except Exception:  # noqa: BLE001
                pass
            for spec in specs:
                res = backfill_table(
                    spec=spec,
                    sqlite_conn=sqlite_conn,
                    pg_conn=pg_conn,
                    schema=schema,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    apply_duplicates_ok=apply_duplicates_ok,
                    execute_values=execute_values,
                )
                results.append(res)
                _print_result(res, dry_run=dry_run)
                if res.table_error:
                    table_failure = True
    finally:
        sqlite_conn.close()

    elapsed = time.monotonic() - started
    total_inserted = sum(r.inserted for r in results if not r.table_error
                         and not r.skipped_existing)
    total_row_errors = sum(r.per_row_errors for r in results)

    print()
    print("=== summary ===")
    print(f"  tables:           {len(results)}")
    print(f"  total inserted:   {total_inserted}"
          f"{' (projected)' if dry_run else ''}")
    print(f"  per-row errors:   {total_row_errors}")
    print(f"  wall time:        {elapsed:.2f}s")

    if table_failure:
        return 2
    if total_row_errors > 0:
        return 3
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    return run(
        source_sqlite=args.source_sqlite,
        tables=args.table,
        batch_size=args.batch_size,
        schema=args.schema,
        dry_run=args.dry_run,
        apply_duplicates_ok=args.apply_duplicates_ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
