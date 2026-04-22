"""Verify `classify_null` threads `rds_writer` through every budget
call site and the audit-trail `record_fetch` call (Spec 0013 P3).

This closes the gap Codex flagged: budget.check_and_reserve /
settle / release grew `rds_writer` kwargs, but the only real caller
is `classify_null`. Without wiring there, the budget_ledger would
stay SQLite-only in live runs.
"""
from __future__ import annotations

import inspect
from lavandula.reports import budget, db_writer
from lavandula.reports.tools import classify_null


def _signature_params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters.keys())


def test_classify_one_signature_accepts_rds_writer():
    assert "rds_writer" in _signature_params(classify_null._classify_one)


def test_release_reservation_signature_accepts_rds_writer():
    assert "rds_writer" in _signature_params(classify_null._release_reservation)


def test_budget_functions_accept_rds_writer():
    # Safeguard: if future refactors remove the kwarg, the wiring test
    # below would silently pass.
    for fn in (budget.check_and_reserve, budget.settle, budget.release):
        assert "rds_writer" in _signature_params(fn), fn.__name__


def test_classify_null_source_wires_rds_writer_into_budget_calls():
    """Inspect the source to confirm every budget.* call passes the
    `rds_writer` kwarg (Codex round-1 finding #2)."""
    src = inspect.getsource(classify_null)
    # check_and_reserve is called with rds_writer=rds_writer
    assert "rds_writer=rds_writer" in src
    # All three budget calls in the file — be specific per call name.
    assert "budget.check_and_reserve(" in src
    assert "budget.settle(" in src
    assert "budget.release(" in src

    # Assert that none of the three budget call sites appears in the
    # source without being followed within a few lines by rds_writer=.
    for fn_name in ("check_and_reserve", "settle", "release"):
        call_idx = src.find(f"budget.{fn_name}(")
        assert call_idx != -1, fn_name
        snippet = src[call_idx:call_idx + 600]
        assert "rds_writer=" in snippet, (
            f"budget.{fn_name} call site missing rds_writer kwarg"
        )


def test_classify_null_source_wires_rds_writer_into_record_fetch():
    """The `record_fetch` audit call for classify events must also
    mirror to RDS so the fetch_log stays consistent across backends."""
    src = inspect.getsource(classify_null)
    call_idx = src.find("db_writer.record_fetch(")
    assert call_idx != -1
    snippet = src[call_idx:call_idx + 600]
    assert "rds_writer=rds_writer" in snippet


def test_classify_null_constructs_rds_writer_behind_flag():
    """The source must construct `RDSDBWriter` only when
    `LAVANDULA_DUAL_WRITE` is truthy."""
    src = inspect.getsource(classify_null)
    assert "LAVANDULA_DUAL_WRITE" in src
    assert "RDSDBWriter(" in src
    # Flag gate precedes construction.
    gate_idx = src.find("LAVANDULA_DUAL_WRITE")
    ctor_idx = src.find("RDSDBWriter(")
    assert 0 < gate_idx < ctor_idx
