"""Fordham baseline regression (sub-phase 1.7, AC13).

The 2026-04-23 crawl of Fordham University archived 207 PDFs, of which
~198 were junk (coloring pages, worksheets, bar-passage tables, etc.)
that matched only via the over-broad ``/media`` weak path keyword.

With the new taxonomy's three-tier triage + path tiering, extract_candidates
run on the same link corpus should produce <= 15 candidates — the vast
majority of junk filenames score below the reject threshold or fail the
weak-path-requires-backing rule.
"""
from __future__ import annotations

from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "fordham_2026_04_23"


@pytest.fixture()
def fordham_html() -> str:
    return (FIXTURE_DIR / "media_index.html").read_text(encoding="utf-8")


def test_ac13_fordham_candidate_count_le_15(fordham_html: str):
    """After taxonomy triage, Fordham yields <= 15 candidates."""
    from lavandula.reports.candidate_filter import extract_candidates

    candidates = extract_candidates(
        html=fordham_html,
        base_url="https://www.fordham.edu/media/",
        seed_etld1="fordham.edu",
        referring_page_url="https://www.fordham.edu/",
        discovered_via="homepage-link",
        ein="131740451",
    )
    assert len(candidates) <= 15, (
        f"Expected <= 15 candidates from Fordham fixture, got {len(candidates)}: "
        + ", ".join(c.url.rsplit("/", 1)[-1] for c in candidates[:20])
    )


def test_ac13_fordham_true_positives_preserved(fordham_html: str):
    """The 2 human-labeled accept PDFs survive the filter."""
    from lavandula.reports.candidate_filter import extract_candidates

    candidates = extract_candidates(
        html=fordham_html,
        base_url="https://www.fordham.edu/media/",
        seed_etld1="fordham.edu",
        referring_page_url="https://www.fordham.edu/",
        discovered_via="homepage-link",
        ein="131740451",
    )
    urls = {c.url for c in candidates}
    expected_accepts = [
        "Annual_Report_2015_2016.pdf",
        "Fordham-Climate-Action-Plan---Annual-Report.pdf",
    ]
    for fn in expected_accepts:
        found = any(fn in u for u in urls)
        assert found, f"Expected accept PDF {fn} missing from candidates"
