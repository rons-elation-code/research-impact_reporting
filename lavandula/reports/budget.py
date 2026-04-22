"""Classifier budget ledger (AC18, AC18.1).

Two-phase reserve/settle protocol against the `budget_ledger` table:

  1. `check_and_reserve(estimated_cents)` — single BEGIN IMMEDIATE txn:
     SELECT SUM(cents_spent) from budget_ledger (includes outstanding
     preflight rows); if running total + estimated exceeds
     config.CLASSIFIER_BUDGET_CENTS, raise BudgetExceeded. Otherwise
     INSERT a preflight row with sha='preflight' and return its id.
  2. `settle(reservation_id, input_tokens, output_tokens, sha)` —
     second BEGIN IMMEDIATE: UPDATE the preflight row with actual
     tokens / cost / sha. Called AFTER the API response returns.
  3. `release(reservation_id)` — DELETE the preflight row. Called when
     the API call raised or was cancelled (no spend).

Crash recovery: `reconcile_stale_reservations(conn)` runs at startup
and releases any preflight rows that survived a crash between reserve
and settle. v1 is strictly single-threaded; if a future plan
parallelizes, a mutex or single-writer manager is required per the
spec.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import Any

from . import classify as _classify
from . import config

_RDS_SCHEMA = "lava_impact"


class BudgetExceeded(RuntimeError):
    """Classifier budget cap would be exceeded by the next call."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def check_and_reserve(
    conn: sqlite3.Connection,
    *,
    estimated_cents: int,
    classifier_model: str,
    rds_writer: Any = None,
) -> int:
    """Atomic preflight reserve: returns the reservation row id.

    Raises `BudgetExceeded` if `estimated_cents + existing_total` would
    exceed the configured cap. Including outstanding preflight rows in
    the running total is deliberate — a crashed / in-flight call must
    not double-spend the remaining budget.
    """
    if estimated_cents <= 0:
        raise ValueError("estimated_cents must be positive")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COALESCE(SUM(cents_spent), 0) FROM budget_ledger"
        ).fetchone()
        running_total = int(row[0] or 0)
        if running_total + estimated_cents > config.CLASSIFIER_BUDGET_CENTS:
            conn.execute("ROLLBACK")
            raise BudgetExceeded(
                f"budget cap {config.CLASSIFIER_BUDGET_CENTS}c: running {running_total}c + "
                f"est {estimated_cents}c would exceed"
            )
        at_iso = _now_iso()
        cur = conn.execute(
            """
            INSERT INTO budget_ledger
              (at_timestamp, classifier_model, sha256_classified,
               input_tokens, output_tokens, cents_spent, notes)
            VALUES (?, ?, 'preflight', 0, 0, ?, 'reserved')
            """,
            (at_iso, classifier_model, int(estimated_cents)),
        )
        reservation_id = int(cur.lastrowid)
        conn.execute("COMMIT")
        if rds_writer is not None:
            # The RDS row's autoincrement id is independent of SQLite's
            # reservation_id; encode SQLite's id into the notes field
            # so `settle`/`release` can locate the matching RDS row.
            rds_note = f"reserved:{reservation_id}"
            params = (at_iso, classifier_model, int(estimated_cents), rds_note)

            def _do_rds(pg_conn: Any) -> None:
                with pg_conn.cursor() as cur_pg:
                    cur_pg.execute(
                        f"""
                        INSERT INTO {_RDS_SCHEMA}.budget_ledger
                          (at_timestamp, classifier_model, sha256_classified,
                           input_tokens, output_tokens, cents_spent, notes)
                        VALUES (%s, %s, 'preflight', 0, 0, %s, %s)
                        """,
                        params,
                    )

            rds_writer.put(_do_rds)
        return reservation_id
    except BudgetExceeded:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def settle(
    conn: sqlite3.Connection,
    *,
    reservation_id: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    sha256_classified: str,
    rds_writer: Any = None,
) -> None:
    """Convert a preflight reservation to an actual spend record."""
    if len(sha256_classified) != 64:
        raise ValueError("sha256_classified must be 64 hex chars")
    actual_cents = _classify.estimate_cents(
        actual_input_tokens, actual_output_tokens
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE budget_ledger
               SET cents_spent = ?,
                   sha256_classified = ?,
                   input_tokens = ?,
                   output_tokens = ?,
                   notes = 'settled'
             WHERE id = ? AND sha256_classified = 'preflight'
            """,
            (
                actual_cents,
                sha256_classified,
                int(actual_input_tokens),
                int(actual_output_tokens),
                reservation_id,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    if rds_writer is not None:
        rds_preflight_note = f"reserved:{reservation_id}"
        params = (
            actual_cents, sha256_classified,
            int(actual_input_tokens), int(actual_output_tokens),
            rds_preflight_note,
        )

        def _do_rds(pg_conn: Any) -> None:
            with pg_conn.cursor() as cur_pg:
                cur_pg.execute(
                    f"""
                    UPDATE {_RDS_SCHEMA}.budget_ledger
                       SET cents_spent = %s,
                           sha256_classified = %s,
                           input_tokens = %s,
                           output_tokens = %s,
                           notes = 'settled'
                     WHERE notes = %s AND sha256_classified = 'preflight'
                    """,
                    params,
                )

        rds_writer.put(_do_rds)


def release(conn: sqlite3.Connection, *, reservation_id: int, rds_writer: Any = None) -> None:
    """Delete an unsettled preflight row (call on API failure)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM budget_ledger WHERE id = ? AND sha256_classified = 'preflight'",
            (reservation_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    if rds_writer is not None:
        rds_preflight_note = f"reserved:{reservation_id}"

        def _do_rds(pg_conn: Any) -> None:
            with pg_conn.cursor() as cur_pg:
                cur_pg.execute(
                    f"""
                    DELETE FROM {_RDS_SCHEMA}.budget_ledger
                     WHERE notes = %s AND sha256_classified = 'preflight'
                    """,
                    (rds_preflight_note,),
                )

        rds_writer.put(_do_rds)


def reconcile_stale_reservations(conn: sqlite3.Connection) -> int:
    """Release any preflight rows at startup (crash between reserve+settle).

    Returns the number of rows reclaimed. v1 is single-threaded, so
    every outstanding preflight row at startup is by definition stale.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM budget_ledger WHERE sha256_classified = 'preflight'"
        )
        conn.execute("COMMIT")
        return cur.rowcount or 0
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


__all__ = [
    "BudgetExceeded",
    "check_and_reserve",
    "settle",
    "release",
    "reconcile_stale_reservations",
]
