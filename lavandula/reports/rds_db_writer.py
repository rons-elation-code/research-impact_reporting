"""Single-thread async writer to RDS Postgres for dual-write (Spec 0013 Phase 3).

Mirrors the interface of `DBWriter` (SQLite) but with a fundamentally
different failure model: **RDS is best-effort**. A full queue, a
crashed worker thread, or a raised psycopg2 exception is logged as
WARNING and dropped — the authoritative SQLite write path is never
affected.

The worker thread opens raw psycopg2 connections via
`engine.raw_connection()` and hands them to each submitted closure.
The closure is expected to run Postgres-flavored SQL (`%s`
placeholders, `lava_impact.<table>` schema qualification,
`ON CONFLICT ... DO UPDATE` clauses).

Key differences from `DBWriter`:
  - `put()` never raises DBWriterSaturated / DBWriterDied; queue
    saturation or thread death → WARN log + drop (best-effort).
  - Per-closure failures rollback and log WARNING; the worker keeps
    going (does not exit on first error).
  - `stop()` drains with a timeout; ops remaining at the deadline are
    logged as drift and dropped.
"""
from __future__ import annotations

import logging
import threading
from queue import Empty, Full, Queue
from typing import Any, Callable, Optional

log = logging.getLogger("lavandula.reports.rds_db_writer")

RDSWriteOp = Callable[[Any], None]


class RDSDBWriter:
    """Async, best-effort RDS writer for dual-write.

    Closures submitted via `put()` run on a single dedicated thread,
    each receiving a raw psycopg2 connection (via
    `engine.raw_connection()`). The closure commits by returning
    normally; the writer rolls back on exception.
    """

    def __init__(
        self,
        engine: Any,
        *,
        maxsize: int = 256,
        name: str = "lavandula-reports-rds-writer",
    ) -> None:
        self._engine = engine
        self._q: "Queue[RDSWriteOp]" = Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._name = name
        self._dropped_on_put = 0
        self._failed_ops = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("RDSDBWriter already started")
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 30.0) -> None:
        """Signal shutdown; drain queue with `timeout` seconds.

        Ops remaining past the deadline are logged as drift and dropped.
        Never raises.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning(
                    "rds writer did not finish draining within %.1fs; "
                    "remaining queue size=%d, dropping on exit",
                    timeout, self._q.qsize(),
                )
            self._thread = None
        remaining = self._q.qsize()
        if remaining:
            log.warning(
                "rds writer stop: %d ops remained in queue (drift)", remaining,
            )

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- submission -------------------------------------------------------

    def put(self, op: RDSWriteOp, *, timeout: float = 30.0) -> None:
        """Enqueue a Postgres closure. Best-effort: never raises.

        If the queue is full for `timeout` seconds, or the writer
        thread is dead, the op is dropped with a WARNING log.
        """
        if self._thread is not None and not self._thread.is_alive():
            self._dropped_on_put += 1
            log.warning(
                "rds writer thread not alive; dropping op "
                "(total dropped=%d)",
                self._dropped_on_put,
            )
            return
        try:
            self._q.put(op, timeout=timeout)
        except Full:
            self._dropped_on_put += 1
            log.warning(
                "rds writer queue saturated for %.1fs; dropping op "
                "(total dropped=%d)",
                timeout, self._dropped_on_put,
            )

    # -- observability ----------------------------------------------------

    @property
    def dropped_count(self) -> int:
        return self._dropped_on_put

    @property
    def failed_count(self) -> int:
        return self._failed_ops

    # -- internal ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                op = self._q.get(timeout=0.25)
            except Empty:
                continue
            self._run_one(op)

    def _run_one(self, op: RDSWriteOp) -> None:
        raw_conn = None
        try:
            raw_conn = self._engine.raw_connection()
        except Exception as exc:  # noqa: BLE001
            self._failed_ops += 1
            log.warning(
                "rds writer: raw_connection() failed (%s); dropping op",
                exc.__class__.__name__,
            )
            return
        try:
            op(raw_conn)
            try:
                raw_conn.commit()
            except Exception as commit_exc:  # noqa: BLE001
                self._failed_ops += 1
                log.warning(
                    "rds writer: commit failed (%s); op dropped",
                    commit_exc.__class__.__name__,
                )
                try:
                    raw_conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            self._failed_ops += 1
            log.warning(
                "rds writer: op failed (%s); rolled back, crawler continues",
                exc.__class__.__name__,
            )
            try:
                raw_conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                raw_conn.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["RDSDBWriter", "RDSWriteOp"]
