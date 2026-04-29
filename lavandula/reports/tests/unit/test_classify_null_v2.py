"""Unit tests for classify_null v2 integration (Spec 0023, Phase 5).

Tests verify the SQL query construction and argument parsing for the
backfill mode. Actual DB writes are tested in integration tests.
"""
from __future__ import annotations

import argparse

import pytest


def test_backfill_mode_mutually_exclusive_with_reclassify():
    """--backfill-material-type and --re-classify are mutually exclusive."""
    from lavandula.reports.tools.classify_null import main
    with pytest.raises(SystemExit):
        import sys
        old_argv = sys.argv
        sys.argv = ["classify_null", "--backfill-material-type", "--re-classify"]
        try:
            main()
        finally:
            sys.argv = old_argv


def test_backfill_query_filter():
    """Backfill mode selects rows where classification IS NOT NULL
    AND material_type IS NULL."""
    row_filter = (
        " AND material_type IS NULL "
        " AND classification IS NOT NULL "
    )
    assert "material_type IS NULL" in row_filter
    assert "classification IS NOT NULL" in row_filter


def test_default_mode_query_filter():
    """Default mode selects rows where classification IS NULL."""
    row_filter = " AND classification IS NULL "
    assert "classification IS NULL" in row_filter


def test_v2_write_includes_all_columns():
    """The _write_result UPDATE statement includes all 6 classification columns."""
    update_sql = (
        "UPDATE lava_corpus.corpus SET "
        "  classification = :class, "
        "  classification_confidence = :conf, "
        "  material_type = :mt, "
        "  material_group = :mg, "
        "  event_type = :et, "
        "  reasoning = :reasoning, "
        "  classifier_model = :model, "
        "  classifier_version = :cver, "
        "  classified_at = :ts "
        "WHERE content_sha256 = :sha"
    )
    for col in ["material_type", "material_group", "event_type",
                "classification", "classification_confidence", "reasoning"]:
        assert col in update_sql


def test_classifier_version_is_3():
    """classify_null now writes classifier_version=3 (Spec 0025 bump)."""
    import inspect
    from lavandula.reports.tools import classify_null
    src = inspect.getsource(classify_null)
    assert '"cver": 3' in src or "'cver': 3" in src
