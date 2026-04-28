"""Classifier budget ledger backed by Postgres (Spec 0017).

Two-phase reserve/settle protocol against `lava_corpus.budget_ledger`:

  1. `check_and_reserve(engine, estimated_cents, ...)` — inside a single
     transaction: `SELECT pg_advisory_xact_lock(...)` to serialize the
     critical section, then `SELECT COALESCE(SUM(cents_spent), 0)` and
     `INSERT` a preflight row. Returns the row id.
  2. `settle(engine, reservation_id, ...)` — UPDATE the preflight row
     with actual tokens / cost / sha.
  3. `release(engine, reservation_id)` — DELETE the preflight row.

Atomicity relies on `pg_advisory_xact_lock(BUDGET_LEDGER_RESERVE)`
(not `SELECT ... FOR UPDATE`, which would lock rows but allow new
INSERTs to land outside the lock). The lock auto-releases at
transaction commit/rollback, so there's no leak on crash.

Crash recovery: `reconcile_stale_reservations(engine)` runs at startup
and deletes any preflight rows that survived a crash between reserve
and settle.
"""
from __future__ import annotations

import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from lavandula.common.lock_keys import BUDGET_LEDGER_RESERVE

from . import classify as _classify
from . import config

_SCHEMA = "lava_corpus"


class BudgetExceeded(RuntimeError):
    """Classifier budget cap would be exceeded by the next call."""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def check_and_reserve(
    engine: Engine,
    *,
    estimated_cents: int,
    classifier_model: str,
) -> int:
    """Atomic preflight reserve: returns the reservation row id.

    Raises `BudgetExceeded` if `estimated_cents + running_total` would
    exceed the configured cap. Outstanding preflight rows are included
    in the running total so a crashed / in-flight call does not
    double-spend.
    """
    if estimated_cents <= 0:
        raise ValueError("estimated_cents must be positive")

    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": BUDGET_LEDGER_RESERVE},
        )
        running_total = int(conn.execute(
            text(f"SELECT COALESCE(SUM(cents_spent), 0) "
                 f"FROM {_SCHEMA}.budget_ledger")
        ).scalar() or 0)
        if running_total + estimated_cents > config.CLASSIFIER_BUDGET_CENTS:
            raise BudgetExceeded(
                f"budget cap {config.CLASSIFIER_BUDGET_CENTS}c: "
                f"running {running_total}c + est {estimated_cents}c "
                f"would exceed"
            )
        reservation_id = int(conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.budget_ledger "
                "(at_timestamp, classifier_model, sha256_classified, "
                " input_tokens, output_tokens, cents_spent, notes) "
                "VALUES (:ts, :model, 'preflight', 0, 0, :cents, 'reserved') "
                "RETURNING id"
            ),
            {
                "ts": _now_iso(),
                "model": classifier_model,
                "cents": int(estimated_cents),
            },
        ).scalar())
        return reservation_id


def settle(
    engine: Engine,
    *,
    reservation_id: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    sha256_classified: str,
) -> None:
    """Convert a preflight reservation to an actual spend record."""
    if len(sha256_classified) != 64:
        raise ValueError("sha256_classified must be 64 hex chars")
    actual_cents = _classify.estimate_cents(
        actual_input_tokens, actual_output_tokens
    )
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": BUDGET_LEDGER_RESERVE},
        )
        conn.execute(
            text(
                f"UPDATE {_SCHEMA}.budget_ledger "
                "   SET cents_spent = :cents, "
                "       sha256_classified = :sha, "
                "       input_tokens = :ins, "
                "       output_tokens = :outs, "
                "       notes = 'settled' "
                " WHERE id = :rid AND sha256_classified = 'preflight'"
            ),
            {
                "cents": actual_cents,
                "sha": sha256_classified,
                "ins": int(actual_input_tokens),
                "outs": int(actual_output_tokens),
                "rid": reservation_id,
            },
        )


def release(engine: Engine, *, reservation_id: int) -> None:
    """Delete an unsettled preflight row (call on API failure)."""
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": BUDGET_LEDGER_RESERVE},
        )
        conn.execute(
            text(
                f"DELETE FROM {_SCHEMA}.budget_ledger "
                " WHERE id = :rid AND sha256_classified = 'preflight'"
            ),
            {"rid": reservation_id},
        )


def reconcile_stale_reservations(engine: Engine) -> int:
    """Release any preflight rows at startup. Returns rows reclaimed."""
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": BUDGET_LEDGER_RESERVE},
        )
        result = conn.execute(
            text(
                f"DELETE FROM {_SCHEMA}.budget_ledger "
                " WHERE sha256_classified = 'preflight'"
            )
        )
        return result.rowcount or 0


__all__ = [
    "BudgetExceeded",
    "check_and_reserve",
    "settle",
    "release",
    "reconcile_stale_reservations",
]
