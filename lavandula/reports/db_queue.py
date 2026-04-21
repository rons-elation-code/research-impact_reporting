"""Single-writer SQLite queue for parallel crawls (TICK-002).

Worker threads submit **callables** that take a `sqlite3.Connection`
argument and execute the full read-then-write logic of a `db_writer`
function. A single dedicated writer thread consumes the queue and
runs each callable on its own connection.

This keeps all SQLite writes on one thread (avoiding connection
thread-affinity issues) and keeps multi-statement semantics
(SELECT → compute merge → UPDATE in `upsert_report`) atomic.

The queue is bounded (`maxsize=256`) to apply backpressure. If the
writer thread dies, `is_alive()` returns False and `put()` will raise
so the main thread can abort the run.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from queue import Empty, Full, Queue
from typing import Callable, Optional


log = logging.getLogger("lavandula.reports.db_queue")

WriteOp = Callable[[sqlite3.Connection], None]


class DBWriterDied(RuntimeError):
    """Raised when the writer thread has crashed and the caller tries
    to submit more work. Callers should abort the run."""


class DBWriter:
    """Owns a single SQLite connection on a single thread.

    Usage:
        writer = DBWriter(db_path)
        writer.start()
        writer.put(lambda conn: conn.execute("INSERT ..."))
        ...
        writer.stop()   # flushes queue + re-raises any crash exception
    """

    def __init__(self, db_path: str, *, maxsize: int = 256) -> None:
        self._db_path = str(db_path)
        self._q: Queue[WriteOp] = Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._exc: Optional[BaseException] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("DBWriter already started")
        self._thread = threading.Thread(
            target=self._run,
            name="lavandula-reports-db-writer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown, drain queue, join. Re-raises any writer
        exception on the caller thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._exc is not None:
            exc = self._exc
            self._exc = None
            raise exc

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- submission -------------------------------------------------------

    def put(self, op: WriteOp, *, timeout: float = 30.0) -> None:
        """Submit a write op. Raises DBWriterDied if the writer crashed."""
        if self._thread is not None and not self._thread.is_alive():
            raise DBWriterDied("DB writer thread is not alive")
        try:
            self._q.put(op, timeout=timeout)
        except Full as exc:
            # Check one more time in case the writer died while we waited
            if self._thread is not None and not self._thread.is_alive():
                raise DBWriterDied("DB writer thread died while queue was full") from exc
            raise

    # -- internal ---------------------------------------------------------

    def _run(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
        except Exception as exc:
            self._exc = exc
            return
        try:
            while not self._stop.is_set() or not self._q.empty():
                try:
                    op = self._q.get(timeout=0.25)
                except Empty:
                    continue
                try:
                    op(conn)
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    # Any failure kills the writer — the caller must
                    # decide whether to abort or skip-and-continue.
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    self._exc = exc
                    log.exception("db-writer op failed; writer will exit")
                    return
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["DBWriter", "DBWriterDied", "WriteOp"]
