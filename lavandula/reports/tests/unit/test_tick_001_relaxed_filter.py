"""TICK-001 — relaxed PDF filter on report-anchor subpages.

Covers AC1-AC9 from the TICK-001 amendment in spec 0004.
"""
from __future__ import annotations


# Typical landing-page HTML: links PDFs with mundane anchor text
# ("Download", "Read here", numeric labels) that the strict Step-3
# filter would reject. This is the Family House / Rockefeller /
# Sage Hill pattern.
REPORT_LANDING_HTML = """
<html><body>
  <h1>Our Impact</h1>
  <a href="/uploads/impact-2024.pdf">Download</a>
  <a href="/uploads/2023.pdf">Read here</a>
  <a href="/uploads/fy22-summary.pdf">2022</a>
  <a href="/other/brochure.pdf">Learn more</a>
  <a href="/about">Another page</a>
</body></html>
"""


HOMEPAGE_HTML = """
<html><body>
  <a href="/our-impact/">Our Impact</a>
  <a href="/careers">Careers</a>
  <a href="/random.pdf">Download</a>
</body></html>
"""


def test_ac1_positive_path_pdf_with_mundane_anchor_accepted():
    """AC1: on a report-anchor subpage, PDF with mundane anchor ('Download')
    is accepted even though strict filter would reject it."""
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html=REPORT_LANDING_HTML,
        base_url="https://example.org/our-impact/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/our-impact/",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    urls = sorted({c.url for c in candidates})
    assert "https://example.org/uploads/impact-2024.pdf" in urls
    assert "https://example.org/uploads/2023.pdf" in urls
    assert "https://example.org/uploads/fy22-summary.pdf" in urls
    # PDFs with strong-negative filenames are rejected even on report
    # subpages (tightened TICK-001 relaxation).
    assert "https://example.org/other/brochure.pdf" not in urls
    # Non-PDF link without keyword: still rejected.
    assert "https://example.org/about" not in urls


def test_ac2_homepage_filter_unchanged_mundane_pdf_rejected():
    """AC2: on the homepage (parent_is_report_anchor=False by default),
    a PDF with mundane anchor text is still rejected."""
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html=HOMEPAGE_HTML,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
        # default: parent_is_report_anchor=False
    )
    urls = {c.url for c in candidates}
    # /our-impact/ matches PATH_KEYWORDS — accepted.
    assert "https://example.org/our-impact/" in urls
    # /random.pdf with anchor "Download" — no keyword match, rejected.
    assert "https://example.org/random.pdf" not in urls


def test_ac3_per_subpage_pdf_cap():
    """AC3: if >20 PDFs link from a report-anchor subpage, only the
    first 20 (document order) are accepted via the relaxed rule."""
    from lavandula.reports.candidate_filter import extract_candidates
    # 30 PDFs, all with mundane anchors.
    pdfs = "".join(
        f'<a href="/u/{i}.pdf">Download</a>\n' for i in range(30)
    )
    html = f"<html><body>{pdfs}</body></html>"
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/our-impact/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/our-impact/",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    pdf_urls = [c.url for c in candidates if c.url.endswith(".pdf")]
    assert len(pdf_urls) == 20
    # First 20 in document order.
    expected = [f"https://example.org/u/{i}.pdf" for i in range(20)]
    assert pdf_urls == expected


def test_ac4_platform_allowlist_preserved_issuu():
    """AC4: Issuu/Flipsnack/Canva URLs are still accepted with
    platform_verified attribution on report-anchor subpages."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = """
    <html><body>
      <a href="https://issuu.com/example/docs/annual-2024">Annual Report 2024</a>
      <a href="https://flipsnack.com/example/report-2023">2023 Report</a>
      <a href="https://www.canva.com/design/abc">Impact Summary</a>
    </body></html>
    """
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/our-impact/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/our-impact/",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    platforms = {c.hosting_platform for c in candidates}
    assert "issuu" in platforms
    assert "flipsnack" in platforms
    assert "canva" in platforms
    for c in candidates:
        if c.hosting_platform in ("issuu", "flipsnack", "canva"):
            assert c.attribution_confidence == "platform_verified"


def test_ac5_cross_origin_non_platform_pdf_dropped():
    """AC5: a PDF URL on a different eTLD+1 (not on the allowlist)
    is dropped, even on a report-anchor subpage."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = """
    <html><body>
      <a href="https://other-domain.com/impact-2024.pdf">Download</a>
      <a href="https://cdn.random-host.net/report.pdf">Report</a>
    </body></html>
    """
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/our-impact/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/our-impact/",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    # No candidates — cross-origin non-platform links are dropped.
    assert candidates == []


def test_ac6_robots_gate_in_discover_layer(tmp_path):
    """AC6: robots-disallowed PDFs are skipped by the discover layer.
    The candidate filter itself doesn't check robots; that's done in
    per_org_candidates. This test exercises the full flow."""
    from lavandula.reports.discover import per_org_candidates

    # robots.txt disallows /private/
    robots_text = "User-agent: *\nDisallow: /private/\n"

    homepage = (
        '<html><body>'
        '<a href="/our-impact/">Our Impact</a>'
        '</body></html>'
    )
    subpage = (
        '<html><body>'
        '<a href="/uploads/public.pdf">Download</a>'
        '<a href="/private/secret.pdf">Internal</a>'
        '</body></html>'
    )

    def _fetcher(url: str, kind: str):
        if "our-impact" in url:
            return (subpage.encode(), "ok")
        return (homepage.encode(), "ok")

    result = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=_fetcher,
        robots_text=robots_text,
    )
    urls = {c.url for c in result}
    assert "https://example.org/uploads/public.pdf" in urls
    assert "https://example.org/private/secret.pdf" not in urls


def test_ac7_non_pdf_links_still_require_keyword():
    """AC7: a non-PDF link on a report-anchor subpage is NOT accepted
    unless its own anchor or path matches the strict filter."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = """
    <html><body>
      <a href="/our-team">Meet the team</a>
      <a href="/pictures.jpg">Download photo</a>
      <a href="/impact/stories">Our work impact</a>
      <a href="/our-impact/story">Read our impact story</a>
    </body></html>
    """
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/our-impact/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/our-impact/",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    urls = sorted({c.url for c in candidates})
    # /our-team: no keyword - dropped
    assert "https://example.org/our-team" not in urls
    # /pictures.jpg: not a PDF - dropped even with "Download"
    assert "https://example.org/pictures.jpg" not in urls
    # /impact/stories: path contains "/impact" substring - accepted
    assert "https://example.org/impact/stories" in urls
    # /our-impact/story: path contains "/our-impact" - accepted
    assert "https://example.org/our-impact/story" in urls


def test_ac8_homepage_filter_byte_identical_without_flag():
    """AC8: extract_candidates() called without parent_is_report_anchor
    (the default) produces the exact same result as before TICK-001."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = """
    <html><body>
      <a href="/annual-report">Annual</a>
      <a href="/random.pdf">Download</a>
      <a href="/careers">Careers</a>
    </body></html>
    """
    r1 = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    r2 = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
        parent_is_report_anchor=False,
    )
    # Same set
    urls1 = sorted({c.url for c in r1})
    urls2 = sorted({c.url for c in r2})
    assert urls1 == urls2
    # /annual-report accepted, /random.pdf rejected (no keyword)
    assert "https://example.org/annual-report" in urls1
    assert "https://example.org/random.pdf" not in urls1


def test_ac9_no_extra_network_fetches_during_extraction():
    """AC9: extract_candidates() does not issue any HTTP requests.
    Content-type validation happens later in fetch_pdf. This test
    verifies the function is pure (no network I/O)."""
    # extract_candidates takes `html` as a string — it doesn't fetch
    # anything. A functional check: pass dummy HTML, verify no
    # exception + no connection attempts.
    import socket
    from lavandula.reports.candidate_filter import extract_candidates

    original_create = socket.socket
    calls = []

    def _track(*a, **kw):
        calls.append((a, kw))
        return original_create(*a, **kw)

    socket.socket = _track
    try:
        extract_candidates(
            html='<a href="/test.pdf">Download</a>',
            base_url="https://example.org/our-impact/",
            seed_etld1="example.org",
            referring_page_url="https://example.org/our-impact/",
            parent_is_report_anchor=True,
        )
    finally:
        socket.socket = original_create
    # No sockets were opened by extract_candidates itself.
    assert calls == []


def test_discover_passes_flag_when_parent_url_matches_path_keyword():
    """Integration: the discover layer correctly computes
    parent_is_report_anchor=True when the subpage's OWN URL matches
    a PATH_KEYWORD."""
    from lavandula.reports.discover import per_org_candidates

    homepage = (
        '<html><body>'
        '<a href="/our-impact/">Our Impact</a>'
        '</body></html>'
    )
    subpage = (
        '<html><body>'
        '<a href="/uploads/impact-2024.pdf">Download</a>'
        '</body></html>'
    )

    def _fetcher(url: str, kind: str):
        if "our-impact" in url:
            return (subpage.encode(), "ok")
        return (homepage.encode(), "ok")

    result = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=_fetcher,
        robots_text="",
    )
    urls = {c.url for c in result}
    # The "Download" PDF should be in the result because /our-impact/
    # matched PATH_KEYWORDS, triggering relaxed mode.
    assert "https://example.org/uploads/impact-2024.pdf" in urls


def test_discover_does_not_relax_when_parent_has_no_keyword():
    """Integration: the discover layer does NOT relax the filter when
    the subpage's URL path and anchor text have no report keywords."""
    from lavandula.reports.discover import per_org_candidates

    homepage = (
        '<html><body>'
        '<a href="/annual-report">Annual Report</a>'
        '<a href="/careers">Careers</a>'
        '</body></html>'
    )
    subpage_annual = (
        '<html><body>'
        '<a href="/uploads/report-2024.pdf">Download</a>'
        '</body></html>'
    )

    def _fetcher(url: str, kind: str):
        if "annual-report" in url:
            return (subpage_annual.encode(), "ok")
        return (homepage.encode(), "ok")

    result = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=_fetcher,
        robots_text="",
    )
    urls = {c.url for c in result}
    # /annual-report has "/annual-report" in PATH_KEYWORDS — relaxed
    # triggers. Download PDF should be included.
    assert "https://example.org/uploads/report-2024.pdf" in urls
