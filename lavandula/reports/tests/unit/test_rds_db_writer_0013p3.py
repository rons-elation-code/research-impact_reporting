"""Tests for `lavandula.reports.rds_db_writer.RDSDBWriter` (Spec 0013 P3).

Verifies best-effort failure semantics:
  - queue saturation drops ops with WARN, never raises
  - writer thread death drops subsequent ops with WARN
  - per-op exceptions rollback but the writer keeps going
  - stop() is non-raising and drains with a timeout
"""
from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock

import pytest

from lavandula.reports.rds_db_writer import RDSDBWriter


class _FakePgConn:
    def __init__(self, *, raise_on_cursor: Exception | None = None,
                 raise_on_commit: Exception | None = None):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._raise_on_cursor = raise_on_cursor
        self._raise_on_commit = raise_on_commit
        self.cursor_calls = []

    def cursor(self):
        if self._raise_on_cursor:
            raise self._raise_on_cursor
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        self.cursor_calls.append(cur)
        return cur

    def commit(self):
        if self._raise_on_commit:
            raise self._raise_on_commit
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, conn_factory):
        self._conn_factory = conn_factory
        self.calls = 0

    def raw_connection(self):
        self.calls += 1
        return self._conn_factory()


def _make_writer(engine, **kw):
    w = RDSDBWriter(engine, **kw)
    w.start()
    return w


def test_put_runs_closure_and_commits():
    """Happy path: put(op) executes op(conn) and commits."""
    conn = _FakePgConn()
    engine = _FakeEngine(lambda: conn)
    w = _make_writer(engine)
    ran = threading.Event()

    def op(c):
        assert c is conn
        ran.set()

    w.put(op)
    assert ran.wait(timeout=2.0)
    w.stop(timeout=2.0)
    assert conn.commits == 1
    assert conn.closed is True


def test_op_exception_logs_warning_and_rolls_back(caplog):
    """An op that raises triggers rollback, WARN log, and writer keeps going."""
    conn_a = _FakePgConn()
    conn_b = _FakePgConn()
    conns = iter([conn_a, conn_b])
    engine = _FakeEngine(lambda: next(conns))
    w = _make_writer(engine)

    done = threading.Event()

    def bad(_c):
        raise RuntimeError("boom")

    def good(_c):
        done.set()

    with caplog.at_level(logging.WARNING, logger="lavandula.reports.rds_db_writer"):
        w.put(bad)
        w.put(good)
        assert done.wait(timeout=2.0)
        w.stop(timeout=2.0)

    assert conn_a.rollbacks == 1
    assert conn_a.commits == 0
    assert conn_b.commits == 1
    assert any("op failed" in r.message for r in caplog.records)
    assert w.failed_count == 1


def test_put_on_full_queue_drops_immediately(caplog):
    """When the queue is full, put() drops immediately — NO blocking.

    This is the Phase 3 contract: `put` is non-blocking because it
    runs on the crawler hot path; any wait would propagate RDS
    latency into SQLite-authoritative writes.
    """
    conn = _FakePgConn()
    engine = _FakeEngine(lambda: conn)
    w = RDSDBWriter(engine, maxsize=1)
    block = threading.Event()

    def slow(_c):
        block.wait(timeout=5.0)

    w.start()
    try:
        w.put(slow)            # worker picks this up, blocks on `block`
        time.sleep(0.05)        # let worker drain the queue
        w.put(slow)             # fills the 1-slot queue
        with caplog.at_level(
            logging.WARNING, logger="lavandula.reports.rds_db_writer"
        ):
            start = time.monotonic()
            w.put(slow)         # must return immediately, not block
            elapsed = time.monotonic() - start
        # Non-blocking contract: < 100ms even on a loaded CI box.
        assert elapsed < 0.1, f"put() blocked for {elapsed:.3f}s"
        assert w.dropped_count >= 1
        assert any("queue full" in r.message for r in caplog.records)
    finally:
        block.set()
        w.stop(timeout=2.0)


def test_put_is_non_blocking_under_sustained_saturation():
    """Flood a saturated writer and assert every put returns fast."""
    conn = _FakePgConn()
    engine = _FakeEngine(lambda: conn)
    w = RDSDBWriter(engine, maxsize=1)
    block = threading.Event()

    def slow(_c):
        block.wait(timeout=5.0)

    w.start()
    try:
        w.put(slow)          # worker picks this up
        time.sleep(0.05)
        w.put(slow)          # fills queue
        for _ in range(50):
            t0 = time.monotonic()
            w.put(lambda _c: None)
            # Each call must be non-blocking regardless of queue state.
            assert time.monotonic() - t0 < 0.05
        assert w.dropped_count >= 50
    finally:
        block.set()
        w.stop(timeout=2.0)


def test_put_when_thread_dead_drops_with_warning(caplog, monkeypatch):
    """If the worker thread is not alive, put() logs a drop and returns."""
    conn = _FakePgConn()
    engine = _FakeEngine(lambda: conn)
    w = RDSDBWriter(engine)
    w.start()
    # Force the thread to exit by signalling stop.
    w._stop.set()
    w._thread.join(timeout=2.0)
    assert not w.is_alive()

    with caplog.at_level(logging.WARNING, logger="lavandula.reports.rds_db_writer"):
        w.put(lambda _c: None)
    assert w.dropped_count == 1
    assert any("not alive" in r.message for r in caplog.records)


def test_raw_connection_failure_logs_and_drops(caplog):
    """engine.raw_connection() raising is logged and the op is dropped."""
    def bad_factory():
        raise RuntimeError("no db")

    class _E:
        def raw_connection(self):
            return bad_factory()

    w = _make_writer(_E())
    done = threading.Event()

    with caplog.at_level(logging.WARNING, logger="lavandula.reports.rds_db_writer"):
        w.put(lambda _c: done.set())
        # Give worker time to attempt raw_connection and fail.
        time.sleep(0.3)
        w.stop(timeout=2.0)
    assert any("raw_connection()" in r.message for r in caplog.records)
    assert w.failed_count == 1
    # Op was not actually executed since connection failed.
    assert not done.is_set()


def test_stop_is_idempotent_and_non_raising():
    conn = _FakePgConn()
    engine = _FakeEngine(lambda: conn)
    w = _make_writer(engine)
    w.stop(timeout=1.0)
    # Calling stop again must not raise.
    w.stop(timeout=0.5)
