"""AC23.1 — reports_public view excludes active-content rows.

Also sanity: DDL creates every CHECK and the public view with the
3-filter WHERE clause (attribution, classification confidence,
active-content).
"""
from __future__ import annotations

import sqlite3

import pytest


def test_schema_creates_all_tables(tmp_reports_db):
    conn = tmp_reports_db
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    for expected in (
        "reports",
        "fetch_log",
        "crawled_orgs",
        "deletion_log",
        "budget_ledger",
        "reports_public",
    ):
        assert expected in names, f"missing {expected!r} in schema"


def test_reports_public_excludes_active_content_AC23_1(tmp_reports_db):
    """AC23.1 — view filters out rows with any active-content flag."""
    conn = tmp_reports_db
    # Insert a would-be-public row but with active-content = 1.
    from lavandula.reports.schema import insert_raw_report_for_test
    insert_raw_report_for_test(
        conn,
        content_sha256="a" * 64,
        source_org_ein="000000001",
        attribution_confidence="own_domain",
        classification="annual",
        classification_confidence=0.95,
        pdf_has_javascript=1,
    )
    rows = list(conn.execute("SELECT content_sha256 FROM reports_public"))
    assert rows == [], "JS-flag row must not be visible in reports_public"


def test_reports_public_excludes_low_confidence_AC16_2(tmp_reports_db):
    conn = tmp_reports_db
    from lavandula.reports.schema import insert_raw_report_for_test
    insert_raw_report_for_test(
        conn,
        content_sha256="b" * 64,
        source_org_ein="000000002",
        attribution_confidence="own_domain",
        classification="annual",
        classification_confidence=0.5,
    )
    rows = list(conn.execute("SELECT content_sha256 FROM reports_public"))
    assert rows == []


def test_reports_public_excludes_unverified_attribution_AC12_3(tmp_reports_db):
    conn = tmp_reports_db
    from lavandula.reports.schema import insert_raw_report_for_test
    insert_raw_report_for_test(
        conn,
        content_sha256="c" * 64,
        source_org_ein="000000003",
        attribution_confidence="platform_unverified",
        classification="annual",
        classification_confidence=0.95,
    )
    rows = list(conn.execute("SELECT content_sha256 FROM reports_public"))
    assert rows == []


def test_reports_public_excludes_not_a_report(tmp_reports_db):
    conn = tmp_reports_db
    from lavandula.reports.schema import insert_raw_report_for_test
    insert_raw_report_for_test(
        conn,
        content_sha256="e" * 64,
        source_org_ein="000000005",
        attribution_confidence="own_domain",
        classification="not_a_report",
        classification_confidence=0.99,
    )
    rows = list(conn.execute("SELECT content_sha256 FROM reports_public"))
    assert rows == []


def test_reports_public_includes_clean_row(tmp_reports_db):
    conn = tmp_reports_db
    from lavandula.reports.schema import insert_raw_report_for_test
    insert_raw_report_for_test(
        conn,
        content_sha256="d" * 64,
        source_org_ein="000000004",
        attribution_confidence="own_domain",
        classification="annual",
        classification_confidence=0.95,
    )
    rows = list(conn.execute("SELECT content_sha256 FROM reports_public"))
    assert [r[0] for r in rows] == ["d" * 64]
