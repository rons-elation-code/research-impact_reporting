"""AC26 — spec-to-DDL drift check.

The reports_public view DDL must name the filters claimed in AC12.3 (attribution),
AC16.2 (classification NOT NULL and confidence >= 0.8), and AC23.1
(active-content exclusion). Missing filters fail this test.
"""
from __future__ import annotations

import pytest


def _view_sql(conn) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='reports_public'"
    ).fetchone()
    assert row is not None, "reports_public view not found"
    return row[0]


def test_ac26_ac12_3_attribution_clause_present(tmp_reports_db):
    sql = _view_sql(tmp_reports_db).lower()
    assert "attribution_confidence" in sql
    assert "own_domain" in sql
    assert "platform_verified" in sql


def test_ac26_ac16_2_classification_confidence_clause_present(tmp_reports_db):
    sql = _view_sql(tmp_reports_db).lower()
    assert "classification is not null" in sql
    assert "classification_confidence" in sql
    assert ">= 0.8" in sql or ">=0.8" in sql


def test_ac26_ac23_1_active_content_clause_present(tmp_reports_db):
    sql = _view_sql(tmp_reports_db).lower()
    assert "pdf_has_javascript = 0" in sql or "pdf_has_javascript=0" in sql
    assert "pdf_has_launch = 0" in sql or "pdf_has_launch=0" in sql
    assert "pdf_has_embedded = 0" in sql or "pdf_has_embedded=0" in sql
