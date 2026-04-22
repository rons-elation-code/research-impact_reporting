"""Tests for `lavandula.common.tools.verify_dual_write` (Spec 0013 P3)."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from lavandula.common.tools import verify_dual_write as vd


def _make_sqlite(tmp_path, rows_by_table):
    """Create a SQLite DB with given tables and row lists.
    rows_by_table: dict[table_name, (pk_col, [pk_values])]
    """
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    for table, (pk_col, values) in rows_by_table.items():
        conn.execute(f"CREATE TABLE {table} ({pk_col} TEXT PRIMARY KEY)")
        for v in values:
            conn.execute(f"INSERT INTO {table} ({pk_col}) VALUES (?)", (v,))
    conn.commit()
    conn.close()
    return str(path)


class _FakeCursor:
    def __init__(self, table_rows):
        """table_rows: dict[table, list[pk]]"""
        self._tables = table_rows
        self._queue = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        import re
        # Count query: SELECT COUNT(*) FROM "schema"."table"
        m = re.search(
            r'SELECT COUNT\(\*\) FROM "[^"]+"\."([^"]+)"',
            sql,
        )
        if m:
            t = m.group(1)
            n = len(self._tables.get(t, []))
            self._queue = [(n,)]
            return
        # PK list: SELECT "<pk>" FROM "<schema>"."<table>" ORDER BY "<pk>" LIMIT %s
        m = re.search(
            r'SELECT "[^"]+" FROM "[^"]+"\."([^"]+)"',
            sql,
        )
        if m:
            t = m.group(1)
            rows = self._tables.get(t, [])
            self._queue = [(v,) for v in rows]
            return
        self._queue = []

    def fetchone(self):
        return self._queue[0] if self._queue else None

    def fetchall(self):
        return list(self._queue)


class _FakePgConn:
    def __init__(self, table_rows):
        self._t = table_rows

    def cursor(self):
        return _FakeCursor(self._t)


class _FakeSA:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def connection(self):
        return self._conn


class _FakeEngine:
    def __init__(self, pg_conn):
        self._pg_conn = pg_conn

    def connect(self):
        return _FakeSA(self._pg_conn)


def test_no_drift(tmp_path, capsys):
    sqlite_path = _make_sqlite(tmp_path, {
        "nonprofits_seed": ("ein", ["a", "b"]),
        "reports":         ("content_sha256", ["s1"]),
        "crawled_orgs":    ("ein", ["a"]),
        "runs":            ("run_id", ["r1"]),
        "fetch_log":       ("ein", ["a", "b"]),
        "deletion_log":    ("ein", []),
        "budget_ledger":   ("ein", []),
    })
    rds_rows = {
        "nonprofits_seed": ["a", "b"],
        "reports":         ["s1"],
        "crawled_orgs":    ["a"],
        "runs":            ["r1"],
        "fetch_log":       ["x", "y"],
        "deletion_log":    [],
        "budget_ledger":   [],
    }
    engine = _FakeEngine(_FakePgConn(rds_rows))
    rc = vd.run(
        sqlite_path=sqlite_path, tables=[], engine_factory=lambda: engine,
    )
    assert rc == 0


def test_drift_detected(tmp_path, capsys):
    sqlite_path = _make_sqlite(tmp_path, {
        "nonprofits_seed": ("ein", ["a", "b", "c"]),
    })
    rds_rows = {"nonprofits_seed": ["a"]}  # missing b, c
    engine = _FakeEngine(_FakePgConn(rds_rows))
    rc = vd.run(
        sqlite_path=sqlite_path,
        tables=["nonprofits_seed"],
        engine_factory=lambda: engine,
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "sqlite count:" in out
    assert "rds count:" in out
    # b and c missing in rds
    assert "missing in rds" in out


def test_unknown_table_raises(tmp_path):
    sqlite_path = _make_sqlite(tmp_path, {})
    with pytest.raises(SystemExit):
        vd.run(
            sqlite_path=sqlite_path,
            tables=["totally_made_up"],
            engine_factory=lambda: _FakeEngine(_FakePgConn({})),
        )


def test_hard_engine_error_returns_two(tmp_path):
    """Docstring promises exit 2 on hard (connection-level) errors."""
    sqlite_path = _make_sqlite(tmp_path, {
        "reports": ("content_sha256", ["s1"]),
    })

    def _raising_factory():
        raise RuntimeError("no engine")

    rc = vd.run(
        sqlite_path=sqlite_path,
        tables=["reports"],
        engine_factory=_raising_factory,
    )
    assert rc == 2


def test_missing_sqlite_table_is_not_drift(tmp_path, capsys):
    """Running against reports.db which doesn't have nonprofits_seed
    should skip (sqlite_table_absent) rather than report drift."""
    sqlite_path = _make_sqlite(tmp_path, {
        "reports": ("content_sha256", ["s1"]),
    })
    rds_rows = {"reports": ["s1"], "nonprofits_seed": ["x"]}
    engine = _FakeEngine(_FakePgConn(rds_rows))
    rc = vd.run(
        sqlite_path=sqlite_path,
        tables=["reports", "nonprofits_seed"],
        engine_factory=lambda: engine,
    )
    # reports matches, nonprofits_seed absent → rc 0
    assert rc == 0
