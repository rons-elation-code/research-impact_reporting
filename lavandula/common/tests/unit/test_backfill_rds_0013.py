"""Unit tests for `lavandula.common.tools.backfill_rds` (Spec 0013 Phase 2).

All DB I/O is mocked. A real tempfile SQLite is used for the source side
(`sqlite3` is stdlib and fast); the Postgres side is a FakePgConn that
records calls and serves canned information_schema / COUNT results.
`execute_values` is likewise injected.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from typing import Any, Callable

import pytest

from lavandula.common.tools import backfill_rds as bf


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakePgCursor:
    def __init__(self, pg: "FakePgConn") -> None:
        self.pg = pg
        self._result: list[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.pg.queries.append((sql, params))
        norm = " ".join(sql.split())
        if norm.startswith("SELECT column_name FROM information_schema.columns"):
            _schema, table = params
            cols = self.pg.columns_by_table.get(table, [])
            self._result = [(c,) for c in cols]
            return
        if "COUNT(*)" in norm:
            matched = False
            for tname, cnt in self.pg.counts.items():
                if f'"{tname}"' in sql:
                    self._result = [(cnt,)]
                    matched = True
                    break
            if not matched:
                self._result = [(0,)]
            return
        self._result = []

    def fetchall(self) -> list[tuple]:
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self) -> "FakePgCursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class FakePgConn:
    def __init__(
        self,
        *,
        columns_by_table: dict[str, list[str]] | None = None,
        counts: dict[str, int] | None = None,
        fail_on_count_for: set[str] | None = None,
    ) -> None:
        self.columns_by_table = columns_by_table or {}
        self.counts = counts or {}
        self.fail_on_count_for = fail_on_count_for or set()
        self.queries: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = True

    def cursor(self) -> FakePgCursor:
        cur = FakePgCursor(self)
        # Allow forced failures for specific tables' COUNT queries
        if self.fail_on_count_for:
            orig_execute = cur.execute

            def patched_execute(sql: str, params: Any = None) -> None:
                if "COUNT(*)" in sql:
                    for t in self.fail_on_count_for:
                        if f'"{t}"' in sql:
                            raise RuntimeError(
                                f"forced count failure for {t}"
                            )
                orig_execute(sql, params)

            cur.execute = patched_execute  # type: ignore[method-assign]
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeSAConn:
    def __init__(self, pg: FakePgConn) -> None:
        self.connection = pg

    def __enter__(self) -> "FakeSAConn":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class FakeEngine:
    def __init__(self, pg: FakePgConn) -> None:
        self._pg = pg

    def connect(self) -> FakeSAConn:
        return FakeSAConn(self._pg)


def _make_execute_values_recorder(
    *, fail_predicate: Callable[[str, list], bool] | None = None,
    track_counts: dict[str, int] | None = None,
) -> tuple[Callable, list[dict]]:
    """Return a fake `execute_values` + a call log.

    `fail_predicate(sql, rows)` returns True to raise, simulating a row
    or batch failure. `track_counts` is a dict from table name to the
    running count; each successful call bumps it by len(rows).
    """
    calls: list[dict] = []

    def _ev(cur, sql, rows, page_size=None, template=None):
        rows = list(rows)
        calls.append({
            "sql": sql,
            "rows": rows,
            "page_size": page_size,
        })
        if fail_predicate and fail_predicate(sql, rows):
            raise RuntimeError("simulated execute_values failure")
        if track_counts is not None:
            for name in list(track_counts):
                if f'"{name}"' in sql:
                    track_counts[name] = track_counts.get(name, 0) + len(rows)
                    break
        return None

    return _ev, calls


# ---------------------------------------------------------------------------
# Fixtures: source sqlite with known tables
# ---------------------------------------------------------------------------

_SEED_DDL = """
CREATE TABLE nonprofits_seed (
    ein TEXT PRIMARY KEY,
    name TEXT,
    state TEXT,
    extra_source_only TEXT
);
"""

_FETCH_LOG_DDL = """
CREATE TABLE fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ein TEXT,
    url_redacted TEXT,
    kind TEXT,
    fetch_status TEXT,
    fetched_at TEXT
);
"""


@pytest.fixture()
def sqlite_path(tmp_path):
    p = tmp_path / "src.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SEED_DDL + _FETCH_LOG_DDL)
    conn.executemany(
        "INSERT INTO nonprofits_seed (ein, name, state, extra_source_only) "
        "VALUES (?,?,?,?)",
        [
            ("001", "Alpha", "NY", "s1"),
            ("002", "Beta",  "TX", "s2"),
            ("003", "Gamma", "CA", "s3"),
        ],
    )
    conn.executemany(
        "INSERT INTO fetch_log (ein, url_redacted, kind, fetch_status, "
        "fetched_at) VALUES (?,?,?,?,?)",
        [
            ("001", "https://a", "homepage", "ok", "2026-01-01T00:00:00Z"),
            ("002", "https://b", "homepage", "ok", "2026-01-01T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()
    return str(p)


def _default_pg(
    *,
    seed_count: int = 0,
    fetch_log_count: int = 0,
    target_only_extra: bool = True,
) -> FakePgConn:
    seed_cols = ["ein", "name", "state"]
    if target_only_extra:
        seed_cols.append("target_only_col")
    return FakePgConn(
        columns_by_table={
            "nonprofits_seed": seed_cols,
            "fetch_log": [
                "id", "ein", "url_redacted", "kind",
                "fetch_status", "fetched_at",
            ],
        },
        counts={
            "nonprofits_seed": seed_count,
            "fetch_log": fetch_log_count,
        },
    )


# ---------------------------------------------------------------------------
# CLI arg parsing
# ---------------------------------------------------------------------------


def test_argv_requires_exactly_one_of_dry_run_or_apply():
    with pytest.raises(SystemExit):
        bf._parse_args(["--source-sqlite", "x.db"])
    with pytest.raises(SystemExit):
        bf._parse_args([
            "--source-sqlite", "x.db", "--dry-run", "--apply",
        ])


def test_argv_source_sqlite_required():
    with pytest.raises(SystemExit):
        bf._parse_args(["--dry-run"])


def test_argv_accepts_repeated_table_flag():
    ns = bf._parse_args([
        "--source-sqlite", "x.db",
        "--table", "nonprofits_seed",
        "--table", "reports",
        "--dry-run",
    ])
    assert ns.table == ["nonprofits_seed", "reports"]


# ---------------------------------------------------------------------------
# Column intersection + alignment
# ---------------------------------------------------------------------------


def test_column_intersection_skips_missing_sides(sqlite_path, caplog):
    caplog.set_level(logging.INFO, logger=bf.log.name)
    pg = _default_pg()
    ev, calls = _make_execute_values_recorder(track_counts={
        "nonprofits_seed": 0,
    })

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    assert len(calls) == 1
    sql = calls[0]["sql"]
    # Only the intersection columns appear.
    assert '"ein"' in sql and '"name"' in sql and '"state"' in sql
    assert "extra_source_only" not in sql
    assert "target_only_col" not in sql
    # Logging at INFO notes both sides.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("source-only columns" in m and "extra_source_only" in m
               for m in msgs)
    assert any("target-only columns" in m and "target_only_col" in m
               for m in msgs)


def test_mismatched_table_logs_info(sqlite_path, caplog):
    caplog.set_level(logging.INFO, logger=bf.log.name)
    pg = _default_pg()
    ev, _calls = _make_execute_values_recorder(track_counts={
        "nonprofits_seed": 0,
    })
    bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "extra_source_only" in joined


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_counts_rows_no_writes(sqlite_path, capsys):
    pg = _default_pg(seed_count=1)
    ev, calls = _make_execute_values_recorder()

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=True,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    assert calls == []  # no writes
    out = capsys.readouterr().out
    assert "would insert" in out
    assert pg.commits == 0


# ---------------------------------------------------------------------------
# Apply path
# ---------------------------------------------------------------------------


def test_apply_inserts_rows_via_execute_values(sqlite_path):
    pg = _default_pg()
    ev, calls = _make_execute_values_recorder(track_counts={
        "nonprofits_seed": 0,
    })

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    assert len(calls) == 1
    rows = calls[0]["rows"]
    assert len(rows) == 3
    # Rows come in as tuples from sqlite; values preserved in col order.
    assert set(tuple(r) for r in rows) == {
        ("001", "Alpha", "NY"),
        ("002", "Beta", "TX"),
        ("003", "Gamma", "CA"),
    }
    assert pg.commits >= 1


def test_on_conflict_skips_existing_rows(sqlite_path):
    pg = _default_pg()
    ev, calls = _make_execute_values_recorder(track_counts={
        "nonprofits_seed": 0,
    })
    bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    sql = calls[0]["sql"]
    assert 'ON CONFLICT ("ein") DO NOTHING' in sql


# ---------------------------------------------------------------------------
# Auto-id tables
# ---------------------------------------------------------------------------


def test_auto_id_tables_omit_id_column(sqlite_path):
    pg = _default_pg()
    ev, calls = _make_execute_values_recorder(track_counts={
        "fetch_log": 0,
    })

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["fetch_log"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    assert len(calls) == 1
    sql = calls[0]["sql"]
    # id column must NOT appear in the column list — quoted or bare.
    assert '"id"' not in sql
    # No ON CONFLICT for auto-id tables.
    assert "ON CONFLICT" not in sql
    # Each row must have one fewer value than the full source column set.
    assert all(len(r) == 5 for r in calls[0]["rows"])  # 5 non-id cols


def test_auto_id_skip_when_dest_has_rows(sqlite_path, caplog):
    caplog.set_level(logging.WARNING, logger=bf.log.name)
    pg = _default_pg(fetch_log_count=7)
    ev, calls = _make_execute_values_recorder()

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["fetch_log"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    assert calls == []  # no insertion
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "auto-id" in joined
    assert "skipping" in joined


def test_auto_id_apply_duplicates_ok_forces_insert(sqlite_path):
    pg = _default_pg(fetch_log_count=7)
    ev, calls = _make_execute_values_recorder(track_counts={
        "fetch_log": 7,
    })

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["fetch_log"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=True,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    assert len(calls) == 1
    assert len(calls[0]["rows"]) == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_per_row_error_continues_batch(sqlite_path):
    """A batch failure triggers row-by-row retry; one bad row yields exit 3."""
    pg = _default_pg()
    # Simulate: batch (len > 1) fails; within row-by-row retry, the row
    # whose ein='002' fails; others succeed.
    def fail_pred(sql, rows):
        if len(rows) > 1:
            return True
        row = rows[0]
        return row[0] == "002"

    ev, calls = _make_execute_values_recorder(
        fail_predicate=fail_pred,
        track_counts={"nonprofits_seed": 0},
    )

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 3  # per-row errors > 0
    # 1 batch call + 3 row-by-row calls = 4
    assert len(calls) == 4
    assert pg.rollbacks >= 2  # failed batch + failed row
    assert pg.commits >= 1


def test_per_table_error_moves_to_next(sqlite_path):
    """If nonprofits_seed's Postgres COUNT errors out, we proceed to
    fetch_log and the run reports exit 2."""
    pg = FakePgConn(
        columns_by_table={
            "nonprofits_seed": ["ein", "name", "state"],
            "fetch_log": [
                "id", "ein", "url_redacted", "kind",
                "fetch_status", "fetched_at",
            ],
        },
        counts={"nonprofits_seed": 0, "fetch_log": 0},
        fail_on_count_for={"nonprofits_seed"},
    )
    ev, calls = _make_execute_values_recorder(track_counts={
        "fetch_log": 0,
    })

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed", "fetch_log"],
        batch_size=100,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 2
    # fetch_log still got its insert call.
    assert len(calls) == 1
    assert '"fetch_log"' in calls[0]["sql"]


# ---------------------------------------------------------------------------
# Batch size
# ---------------------------------------------------------------------------


def test_batch_size_honored(sqlite_path):
    pg = _default_pg()
    ev, calls = _make_execute_values_recorder(track_counts={
        "nonprofits_seed": 0,
    })

    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=2,
        schema="lava_impact",
        dry_run=False,
        apply_duplicates_ok=False,
        engine_factory=lambda: FakeEngine(pg),
        execute_values=ev,
    )
    assert rc == 0
    # 3 rows with batch_size=2 → two batches (2 + 1).
    assert [len(c["rows"]) for c in calls] == [2, 1]
    assert all(c["page_size"] == 2 for c in calls)


# ---------------------------------------------------------------------------
# Identifier safety
# ---------------------------------------------------------------------------


def test_unsafe_schema_rejected(sqlite_path):
    with pytest.raises(ValueError):
        bf.run(
            source_sqlite=sqlite_path,
            tables=["nonprofits_seed"],
            batch_size=100,
            schema="bad; DROP TABLE",
            dry_run=True,
            apply_duplicates_ok=False,
            engine_factory=lambda: FakeEngine(_default_pg()),
            execute_values=lambda *a, **k: None,
        )


def test_unknown_table_name_rejected():
    with pytest.raises(SystemExit):
        bf._resolve_table_specs(["no_such_table"])


# ---------------------------------------------------------------------------
# Factory plumbing
# ---------------------------------------------------------------------------


def test_uses_app_engine_by_default(monkeypatch, sqlite_path):
    """`run(engine_factory=None)` must call `make_app_engine`, not
    anything master-level."""
    called = {"n": 0}
    pg = _default_pg()

    def fake_app_engine():
        called["n"] += 1
        return FakeEngine(pg)

    monkeypatch.setattr(
        "lavandula.common.db.make_app_engine", fake_app_engine, raising=True,
    )
    ev, _calls = _make_execute_values_recorder(track_counts={
        "nonprofits_seed": 0,
    })
    rc = bf.run(
        source_sqlite=sqlite_path,
        tables=["nonprofits_seed"],
        batch_size=100,
        schema="lava_impact",
        dry_run=True,
        apply_duplicates_ok=False,
        engine_factory=None,  # triggers default resolution
        execute_values=ev,
    )
    assert rc == 0
    assert called["n"] == 1
