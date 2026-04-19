"""AC23 — public view enforcement (grep); AC24 — deterministic latest-per-org."""
from __future__ import annotations

import pathlib

import pytest


REPORTS_PKG = pathlib.Path(__file__).parent.parent.parent


def test_ac23_grep_forbids_raw_reports_outside_whitelist():
    """AC23 — lavandula/reports/ may not reference FROM reports
    outside catalogue.py / db_writer.py / schema.py.

    TICK-003 adds tools/classify_null.py to the whitelist as it's
    an admin tool that writes classification data — the very data
    the reports_public view filters by — so must bypass the view.
    """
    whitelist = {"catalogue.py", "db_writer.py", "schema.py",
                 "classify_null.py"}
    # Tests directory is excluded — they inspect the DB intentionally.
    bad: list[tuple[str, int, str]] = []
    for py in REPORTS_PKG.rglob("*.py"):
        rel = py.relative_to(REPORTS_PKG)
        if rel.parts[0] == "tests":
            continue
        if py.name in whitelist:
            continue
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            low = line.lower()
            # ignore comments and the string 'reports_public'
            if "from reports" in low and "reports_public" not in low:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                bad.append((str(rel), lineno, line))
    assert not bad, f"Raw FROM reports outside whitelist: {bad}"


def test_ac24_latest_per_org_max_year(tmp_reports_db):
    from lavandula.reports.catalogue import latest_report_per_org
    from lavandula.reports.schema import insert_raw_report_for_test
    ein = "000000001"
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256="1" * 64,
        source_org_ein=ein,
        report_year=2022,
        classification="annual",
        classification_confidence=0.9,
        attribution_confidence="own_domain",
        archived_at="2022-06-01T00:00:00Z",
    )
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256="2" * 64,
        source_org_ein=ein,
        report_year=2024,
        classification="annual",
        classification_confidence=0.9,
        attribution_confidence="own_domain",
        archived_at="2024-06-01T00:00:00Z",
    )
    row = latest_report_per_org(tmp_reports_db, ein=ein)
    assert row["content_sha256"] == "2" * 64


def test_ac24_latest_per_org_null_year_loses(tmp_reports_db):
    """NULL report_year sorts LAST (NULLS LAST)."""
    from lavandula.reports.catalogue import latest_report_per_org
    from lavandula.reports.schema import insert_raw_report_for_test
    ein = "000000001"
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256="1" * 64,
        source_org_ein=ein,
        report_year=None,
        classification="annual",
        classification_confidence=0.9,
        attribution_confidence="own_domain",
        archived_at="2024-06-01T00:00:00Z",
    )
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256="2" * 64,
        source_org_ein=ein,
        report_year=2020,
        classification="annual",
        classification_confidence=0.9,
        attribution_confidence="own_domain",
        archived_at="2020-06-01T00:00:00Z",
    )
    row = latest_report_per_org(tmp_reports_db, ein=ein)
    assert row["content_sha256"] == "2" * 64


def test_ac24_tiebreakers_deterministic(tmp_reports_db):
    """Same year → MAX archived_at → MAX confidence → first-seen sha."""
    from lavandula.reports.catalogue import latest_report_per_org
    from lavandula.reports.schema import insert_raw_report_for_test
    ein = "000000001"
    # Two 2024 reports with different archived_at — newer wins.
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256="a" * 64,
        source_org_ein=ein,
        report_year=2024,
        classification_confidence=0.95,
        attribution_confidence="own_domain",
        archived_at="2024-01-01T00:00:00Z",
    )
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256="b" * 64,
        source_org_ein=ein,
        report_year=2024,
        classification_confidence=0.9,
        attribution_confidence="own_domain",
        archived_at="2024-12-01T00:00:00Z",
    )
    assert latest_report_per_org(tmp_reports_db, ein=ein)["content_sha256"] == "b" * 64
