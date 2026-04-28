"""AC15 — budget reserve/settle/release atomic under concurrent callers.

Spawns N threads each calling `check_and_reserve` with an estimate
that would overflow the cap if a single caller exceeded it; asserts
no overflow occurs (the `pg_advisory_xact_lock` serializes the
read-SUM + INSERT critical section).
"""
from __future__ import annotations

import threading

import pytest


pytestmark = pytest.mark.usefixtures("postgres_engine")


def test_budget_reserve_concurrent_callers_no_overflow(postgres_engine, monkeypatch):
    from lavandula.reports import budget, config

    # Set cap so exactly 4 serialized callers fit; a 5th must be rejected.
    monkeypatch.setattr(config, "CLASSIFIER_BUDGET_CENTS", 40)

    successes: list[int] = []
    exceeded: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            rid = budget.check_and_reserve(
                postgres_engine,
                estimated_cents=10,
                classifier_model="claude-haiku-4-5",
            )
            successes.append(rid)
        except budget.BudgetExceeded as exc:
            exceeded.append(str(exc))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors!r}"
    assert len(successes) == 4, (
        f"expected exactly 4 successful reservations, got {len(successes)} "
        f"(exceeded={len(exceeded)})"
    )
    assert len(exceeded) == 2


def test_budget_release_clears_reservation(postgres_engine):
    from lavandula.reports import budget

    rid = budget.check_and_reserve(
        postgres_engine,
        estimated_cents=1,
        classifier_model="claude-haiku-4-5",
    )
    budget.release(postgres_engine, reservation_id=rid)
    reclaimed = budget.reconcile_stale_reservations(postgres_engine)
    # Already released → nothing more to reclaim.
    assert reclaimed == 0


def test_budget_settle_marks_row(postgres_engine):
    from sqlalchemy import text
    from lavandula.reports import budget

    rid = budget.check_and_reserve(
        postgres_engine,
        estimated_cents=5,
        classifier_model="claude-haiku-4-5",
    )
    budget.settle(
        postgres_engine,
        reservation_id=rid,
        actual_input_tokens=100,
        actual_output_tokens=10,
        sha256_classified="a" * 64,
    )
    with postgres_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT sha256_classified, notes "
            "FROM lava_corpus.budget_ledger WHERE id = :i"
        ), {"i": rid}).fetchone()
    assert row[0] == "a" * 64
    assert row[1] == "settled"
