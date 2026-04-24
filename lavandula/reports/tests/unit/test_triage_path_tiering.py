"""Tests for three-tier triage + path tiering (sub-phase 1.5, AC07-AC10)."""
from __future__ import annotations

from lavandula.reports.candidate_filter import extract_candidates


def _candidates_for(html: str, base: str = "https://example.org/"):
    return extract_candidates(
        html=html,
        base_url=base,
        seed_etld1="example.org",
        referring_page_url=base,
    )


# --- Filename triage ---


def test_triage_accept_filename_strong():
    """Strong filename (annual-report + year) passes even without anchor/path."""
    html = '<html><body><a href="/random/annual-report-2024.pdf">Click</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 1


def test_triage_drop_filename_reject():
    """AC07: junk filename on a weak path is dropped pre-fetch."""
    html = '<html><body><a href="/media/Ram_Coloring_Page.pdf">Stuff</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 0


# --- Path tiering ---


def test_triage_weak_path_alone_rejected():
    """AC09: weak path + negative-signal filename (below weak_path_min) → dropped."""
    html = '<html><body><a href="/media/board-meeting-memo.pdf">Click</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 0


def test_triage_weak_path_with_anchor():
    """Weak path with anchor text match → accept."""
    html = '<html><body><a href="/media/x.pdf">annual report</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 1


def test_triage_weak_path_with_filename_support():
    """Weak path with a positive-signal filename → accept (score >= weak_path_min)."""
    html = '<html><body><a href="/media/impact-report-2024.pdf">Download</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 1


def test_triage_strong_path_alone():
    """AC10: strong path alone still causes acceptance."""
    html = '<html><body><a href="/annual-report/x.pdf">Click</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 1


def test_triage_case_insensitive_path():
    """Path matching is case-insensitive per spec."""
    html = '<html><body><a href="/Annual-Report/X.pdf">Click</a></body></html>'
    cs = _candidates_for(html)
    assert len(cs) == 1


def test_triage_fordham_coloring_page_dropped():
    """Full Fordham-style URL: junk filename + weak path → dropped."""
    html = (
        '<html><body>'
        '<a href="https://example.org/media/departments/fordham-university/images/Ram_Coloring_Page.pdf">Fun</a>'
        '</body></html>'
    )
    cs = _candidates_for(html)
    assert len(cs) == 0
