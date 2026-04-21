"""TICK-002 — single-writer SQLite queue (DBWriter)."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from lavandula.reports.db_queue import DBWriter, DBWriterDied, DBWriterSaturated


def _bootstrap_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()
    conn.close()


def test_put_writes_are_serialized(tmp_path):
    db = tmp_path / "q.db"
    _bootstrap_db(db)
    w = DBWriter(str(db))
    w.start()
    try:
        for i in range(50):
            w.put(lambda c, i=i: c.execute("INSERT INTO t (val) VALUES (?)", (str(i),)))
    finally:
        w.stop()

    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert n == 50


def test_writer_exception_is_reraised_on_stop(tmp_path):
    db = tmp_path / "q.db"
    _bootstrap_db(db)
    w = DBWriter(str(db))
    w.start()

    def boom(_conn):
        raise RuntimeError("intentional")

    w.put(boom)
    # Wait for writer to die.
    deadline = time.monotonic() + 2.0
    while w.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(RuntimeError, match="intentional"):
        w.stop()


def test_bounded_queue_applies_backpressure(tmp_path):
    """A small queue + a slow writer + many puts must block, not grow."""
    db = tmp_path / "q.db"
    _bootstrap_db(db)
    w = DBWriter(str(db), maxsize=2)

    gate = threading.Event()

    def slow(_conn):
        gate.wait(timeout=2.0)

    w.start()
    try:
        # Fill the queue and hand the writer a blocking op.
        w.put(slow)
        w.put(lambda c: c.execute("INSERT INTO t (val) VALUES ('a')"))
        w.put(lambda c: c.execute("INSERT INTO t (val) VALUES ('b')"))
        # Next put must time out quickly because the queue is full.
        with pytest.raises(Exception):
            w.put(lambda c: c.execute("INSERT INTO t (val) VALUES ('c')"), timeout=0.1)
    finally:
        gate.set()
        w.stop()


def test_saturation_raises_dbwriter_saturated(tmp_path):
    """Round 5: a full queue on a live-but-slow writer must raise
    DBWriterSaturated, not ordinary queue.Full — so callers that
    `except DBWriterDied` correctly abort the run on saturation."""
    db = tmp_path / "q.db"
    _bootstrap_db(db)
    w = DBWriter(str(db), maxsize=1)

    gate = threading.Event()

    def slow(_conn):
        gate.wait(timeout=2.0)

    w.start()
    try:
        # Block the writer with a gated op, fill the 1-slot queue.
        w.put(slow)
        w.put(lambda c: c.execute("INSERT INTO t (val) VALUES ('x')"))
        with pytest.raises(DBWriterSaturated):
            w.put(lambda c: c.execute("INSERT INTO t (val) VALUES ('y')"),
                  timeout=0.1)
    finally:
        gate.set()
        w.stop()


def test_dbwriter_saturated_is_dbwriter_died_subclass():
    """Callers that catch DBWriterDied to abort the run must also
    catch saturation (pipeline failure = abort)."""
    assert issubclass(DBWriterSaturated, DBWriterDied)


def test_put_after_writer_death_raises(tmp_path):
    db = tmp_path / "q.db"
    _bootstrap_db(db)
    w = DBWriter(str(db))
    w.start()
    w.put(lambda c: (_ for _ in ()).throw(RuntimeError("die")))
    deadline = time.monotonic() + 2.0
    while w.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(DBWriterDied):
        w.put(lambda c: None)
    # Drain so stop()'s re-raise doesn't leak into other tests.
    with pytest.raises(RuntimeError):
        w.stop()
