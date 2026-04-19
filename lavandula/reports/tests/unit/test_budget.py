"""AC18.1 — budget ledger atomicity: reserve / settle / release."""
from __future__ import annotations

import pytest


def test_ac18_1_reserve_insert_row(tmp_reports_db):
    from lavandula.reports.budget import check_and_reserve
    rid = check_and_reserve(tmp_reports_db, estimated_cents=5, classifier_model="claude-haiku-4-5")
    row = tmp_reports_db.execute(
        "SELECT sha256_classified, cents_spent FROM budget_ledger WHERE id = ?",
        (rid,),
    ).fetchone()
    assert row is not None
    assert row[0] == "preflight"
    assert row[1] == 5


def test_ac18_1_reserve_halts_over_budget(tmp_reports_db):
    from lavandula.reports.budget import check_and_reserve, BudgetExceeded
    # Pre-seed spend near the cap.
    from lavandula.reports import config as cfg
    tmp_reports_db.execute(
        "INSERT INTO budget_ledger (at_timestamp, classifier_model, sha256_classified, "
        "input_tokens, output_tokens, cents_spent) VALUES (?,?,?,?,?,?)",
        ("2026-04-19T00:00:00Z", "claude-haiku-4-5", "a" * 64, 1, 1, cfg.CLASSIFIER_BUDGET_CENTS - 1),
    )
    with pytest.raises(BudgetExceeded):
        check_and_reserve(tmp_reports_db, estimated_cents=1000, classifier_model="claude-haiku-4-5")


def test_ac18_1_settle_converts_preflight_to_actual(tmp_reports_db):
    from lavandula.reports.budget import check_and_reserve, settle
    rid = check_and_reserve(tmp_reports_db, estimated_cents=5, classifier_model="claude-haiku-4-5")
    settle(
        tmp_reports_db,
        reservation_id=rid,
        actual_input_tokens=1200,
        actual_output_tokens=80,
        sha256_classified="a" * 64,
    )
    row = tmp_reports_db.execute(
        "SELECT sha256_classified, input_tokens, output_tokens FROM budget_ledger WHERE id = ?",
        (rid,),
    ).fetchone()
    assert row[0] == "a" * 64
    assert row[1] == 1200
    assert row[2] == 80


def test_ac18_1_release_deletes_preflight(tmp_reports_db):
    from lavandula.reports.budget import check_and_reserve, release
    rid = check_and_reserve(tmp_reports_db, estimated_cents=5, classifier_model="claude-haiku-4-5")
    release(tmp_reports_db, reservation_id=rid)
    row = tmp_reports_db.execute(
        "SELECT id FROM budget_ledger WHERE id = ?", (rid,)
    ).fetchone()
    assert row is None


def test_ac18_1_reconcile_stale_reservations(tmp_reports_db):
    """Crash between reserve and settle leaves a 'preflight' row; reconcile should clear it."""
    from lavandula.reports.budget import check_and_reserve, reconcile_stale_reservations
    rid = check_and_reserve(tmp_reports_db, estimated_cents=5, classifier_model="claude-haiku-4-5")
    reconcile_stale_reservations(tmp_reports_db)
    # After reconcile, either the preflight row is released or marked stale.
    row = tmp_reports_db.execute(
        "SELECT sha256_classified FROM budget_ledger WHERE id = ?", (rid,)
    ).fetchone()
    # Either removed entirely or rewritten to a terminal state.
    assert row is None or row[0] != "preflight"
