"""Drift detection between SQLite and RDS dual-write (Spec 0013 Phase 3).

Compares row counts and PK overlap between a local SQLite DB and the
corresponding `lava_impact.<table>` in RDS. Intended as an operational
check during the Phase 3 stabilization period before the Phase 4
read flip.

Usage
-----
    python -m lavandula.common.tools.verify_dual_write \\
        --sqlite PATH/reports.db \\
        [--table TABLE]...

Exit codes
----------
  0  no drift detected (all compared tables match)
  1  drift detected (counts differ or missing PKs in either backend)
  2  hard error (connection-level)
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger("lavandula.common.tools.verify_dual_write")

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


@dataclass(frozen=True)
class TableSpec:
    name: str
    pk: str | None


TABLES: tuple[TableSpec, ...] = (
    TableSpec("nonprofits_seed", pk="ein"),
    TableSpec("reports",         pk="content_sha256"),
    TableSpec("crawled_orgs",    pk="ein"),
    TableSpec("runs",            pk="run_id"),
    TableSpec("fetch_log",       pk=None),
    TableSpec("deletion_log",    pk=None),
    TableSpec("budget_ledger",   pk=None),
)

TABLE_BY_NAME: dict[str, TableSpec] = {t.name: t for t in TABLES}


@dataclass
class TableDrift:
    table: str
    sqlite_count: int = 0
    rds_count: int = 0
    missing_in_rds: list[str] = field(default_factory=list)
    missing_in_sqlite: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def has_drift(self) -> bool:
        if self.error:
            return True
        if self.sqlite_count != self.rds_count:
            return True
        if self.missing_in_rds or self.missing_in_sqlite:
            return True
        return False


def _sqlite_has_table(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.execute(f"SELECT COUNT(*) FROM {_safe_ident(table)}")
    return int(cur.fetchone()[0])


def _postgres_count(pg_cur, schema: str, table: str) -> int:
    pg_cur.execute(
        f'SELECT COUNT(*) FROM "{_safe_ident(schema)}"."{_safe_ident(table)}"'
    )
    return int(pg_cur.fetchone()[0])


def _sqlite_pks(conn: sqlite3.Connection, table: str, pk: str) -> set[str]:
    # Full set-diff (no LIMIT). Current scale (<10K rows) makes the
    # memory cost trivial. If scale grows past ~1M, add an opt-in
    # `--sample-size` flag — but a bounded sample would hide real
    # drift that falls outside the sample, so the default must be
    # exact.
    cur = conn.execute(
        f"SELECT {_safe_ident(pk)} FROM {_safe_ident(table)}"
    )
    return {str(r[0]) for r in cur.fetchall() if r[0] is not None}


def _postgres_pks(pg_cur, schema: str, table: str, pk: str) -> set[str]:
    pg_cur.execute(
        f'SELECT "{_safe_ident(pk)}" '
        f'FROM "{_safe_ident(schema)}"."{_safe_ident(table)}"'
    )
    return {str(r[0]) for r in pg_cur.fetchall() if r[0] is not None}


def verify_table(
    spec: TableSpec,
    *,
    sqlite_conn: sqlite3.Connection,
    pg_conn: Any,
    schema: str,
    sample_size: int = 20,
) -> TableDrift:
    """Compare one table. `sample_size` caps the number of missing
    PKs printed in the drift report; set-diff itself is exhaustive."""
    res = TableDrift(table=spec.name)
    if not _sqlite_has_table(sqlite_conn, spec.name):
        # This SQLite file doesn't have this table; e.g. reports.db vs seeds.db.
        res.error = "sqlite_table_absent"
        return res
    try:
        res.sqlite_count = _sqlite_count(sqlite_conn, spec.name)
    except sqlite3.Error as exc:
        res.error = f"sqlite_error:{exc.__class__.__name__}"
        return res

    try:
        with pg_conn.cursor() as cur:
            res.rds_count = _postgres_count(cur, schema, spec.name)
    except Exception as exc:  # noqa: BLE001
        res.error = f"rds_error:{exc.__class__.__name__}"
        return res

    if spec.pk:
        try:
            sqlite_set = _sqlite_pks(sqlite_conn, spec.name, spec.pk)
            with pg_conn.cursor() as cur:
                rds_set = _postgres_pks(cur, schema, spec.name, spec.pk)
        except Exception as exc:  # noqa: BLE001
            res.error = f"pk_compare_error:{exc.__class__.__name__}"
            return res
        only_sqlite = sorted(sqlite_set - rds_set)[:sample_size]
        only_rds = sorted(rds_set - sqlite_set)[:sample_size]
        res.missing_in_rds = only_sqlite
        res.missing_in_sqlite = only_rds
    return res


def _resolve_specs(requested: Sequence[str]) -> list[TableSpec]:
    if not requested:
        return list(TABLES)
    out: list[TableSpec] = []
    for name in requested:
        if name not in TABLE_BY_NAME:
            raise SystemExit(
                f"unknown table {name!r}; known: {sorted(TABLE_BY_NAME)}"
            )
        out.append(TABLE_BY_NAME[name])
    return out


def _print_drift(d: TableDrift) -> None:
    print(f"=== {d.table} ===")
    if d.error:
        print(f"  ERROR: {d.error}")
        return
    delta = d.rds_count - d.sqlite_count
    sign = f"{delta:+d}"
    print(f"  sqlite count:    {d.sqlite_count}")
    print(f"  rds count:       {d.rds_count}   (drift: {sign})")
    if d.missing_in_rds:
        print(f"  missing in rds:  {', '.join(d.missing_in_rds[:10])}"
              f"{' …' if len(d.missing_in_rds) > 10 else ''}")
    if d.missing_in_sqlite:
        print(f"  missing in sqlite: {', '.join(d.missing_in_sqlite[:10])}"
              f"{' …' if len(d.missing_in_sqlite) > 10 else ''}")


def run(
    *,
    sqlite_path: str,
    tables: Sequence[str],
    schema: str = "lava_impact",
    engine_factory=None,
) -> int:
    if engine_factory is None:
        from lavandula.common.db import make_ro_engine
        engine_factory = make_ro_engine

    _safe_ident(schema)
    specs = _resolve_specs(tables)

    try:
        sqlite_conn = sqlite3.connect(sqlite_path)
    except sqlite3.Error as exc:
        log.error("cannot open sqlite %s: %s", sqlite_path, exc)
        return 2

    try:
        engine = engine_factory()
    except Exception as exc:  # noqa: BLE001
        log.error("cannot build RDS engine: %s", exc.__class__.__name__)
        sqlite_conn.close()
        return 2

    drifts: list[TableDrift] = []
    any_error = False
    try:
        with engine.connect() as sa_conn:
            pg_conn = sa_conn.connection
            try:
                pg_conn.autocommit = True
            except Exception:  # noqa: BLE001
                pass
            for spec in specs:
                d = verify_table(
                    spec,
                    sqlite_conn=sqlite_conn,
                    pg_conn=pg_conn,
                    schema=schema,
                )
                drifts.append(d)
                _print_drift(d)
                if d.error and d.error != "sqlite_table_absent":
                    any_error = True
    finally:
        sqlite_conn.close()

    total_drift = sum(
        1 for d in drifts
        if d.error != "sqlite_table_absent" and d.has_drift
    )
    print()
    print("=== summary ===")
    print(f"  tables compared: {len(drifts)}")
    print(f"  drift count:     {total_drift}")

    if any_error:
        return 2
    return 1 if total_drift else 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m lavandula.common.tools.verify_dual_write",
        description="Compare SQLite vs RDS row counts and PK presence.",
    )
    ap.add_argument("--sqlite", required=True,
                    help="Path to source SQLite DB file")
    ap.add_argument("--table", action="append", default=[],
                    help="Specific table(s); repeatable. Default: all seven.")
    ap.add_argument("--schema", default="lava_impact",
                    help="Target Postgres schema (default lava_impact).")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    return run(
        sqlite_path=args.sqlite,
        tables=args.table,
        schema=args.schema,
    )


if __name__ == "__main__":
    raise SystemExit(main())
