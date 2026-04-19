"""AC16.2 — classifier outage fallback; AC18 — budget cap halt."""
from __future__ import annotations

import pytest


def test_ac16_2_outage_stores_null_classification():
    """On network error / non-JSON / rate-limited, classification stays NULL."""
    from lavandula.reports.classify import classify_first_page, ClassifierError

    class _C:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise TimeoutError("api unreachable")

    result = classify_first_page(
        first_page_text="some text",
        client=_C(),
        raise_on_error=False,
    )
    assert result.classification is None
    assert result.classification_confidence is None
    assert result.error


def test_ac18_budget_cap_halts(tmp_reports_db):
    """Preflight reserve over cap raises BudgetExceeded."""
    from lavandula.reports.budget import check_and_reserve, BudgetExceeded
    from lavandula.reports import config as cfg

    # Preseed near the cap
    tmp_reports_db.execute(
        "INSERT INTO budget_ledger (at_timestamp, classifier_model, sha256_classified, "
        "input_tokens, output_tokens, cents_spent) VALUES (?,?,?,?,?,?)",
        ("2026-04-19T00:00:00Z", "claude-haiku-4-5", "a" * 64, 1, 1, cfg.CLASSIFIER_BUDGET_CENTS),
    )
    with pytest.raises(BudgetExceeded):
        check_and_reserve(tmp_reports_db, estimated_cents=1, classifier_model="claude-haiku-4-5")
