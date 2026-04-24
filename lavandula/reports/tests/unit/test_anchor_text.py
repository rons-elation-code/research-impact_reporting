"""Tests for _effective_anchor_text (sub-phase 1.3, AC04/AC05)."""
from __future__ import annotations

from bs4 import BeautifulSoup


def _make_tag(html_fragment: str):
    soup = BeautifulSoup(html_fragment, "lxml")
    return soup.find("a")


def test_anchor_text_image_only_with_alt():
    """AC04: image-link with alt text is retained."""
    from lavandula.reports.candidate_filter import _effective_anchor_text

    a = _make_tag('<a href="r.pdf"><img alt="2024 Annual Report"></a>')
    text = _effective_anchor_text(a)
    assert "2024 Annual Report" in text


def test_anchor_text_title_attribute():
    """AC05: title attribute contributes."""
    from lavandula.reports.candidate_filter import _effective_anchor_text

    a = _make_tag('<a href="x.pdf" title="Our Impact">Click here</a>')
    text = _effective_anchor_text(a)
    assert "Our Impact" in text
    assert "Click here" in text


def test_anchor_text_aria_label():
    """AC05: aria-label contributes."""
    from lavandula.reports.candidate_filter import _effective_anchor_text

    a = _make_tag('<a href="x.pdf" aria-label="Gala Invitation">Details</a>')
    text = _effective_anchor_text(a)
    assert "Gala Invitation" in text


def test_anchor_text_visible_overrides_nothing():
    """Visible text is concatenated with title/alt, not replaced."""
    from lavandula.reports.candidate_filter import _effective_anchor_text

    a = _make_tag(
        '<a href="x.pdf" title="Impact Report"><img alt="Cover">Read more</a>'
    )
    text = _effective_anchor_text(a)
    assert "Read more" in text
    assert "Impact Report" in text
    assert "Cover" in text


def test_anchor_text_empty_when_all_missing():
    from lavandula.reports.candidate_filter import _effective_anchor_text

    a = _make_tag('<a href="x.pdf"></a>')
    assert _effective_anchor_text(a) == ""


def test_image_link_passes_anchor_filter():
    """AC04: image-link report with alt text passes the candidate filter."""
    from lavandula.reports.candidate_filter import extract_candidates

    html = '<html><body><a href="report.pdf"><img alt="2024 Annual Report"></a></body></html>'
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert len(candidates) == 1
    assert "report.pdf" in candidates[0].url
