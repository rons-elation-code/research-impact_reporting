"""TICK-007 — sitemap-derived HTML URLs matching report patterns
get subpage-expanded the same way homepage-derived URLs do.

Plus a regression test for the c7dd01f hotfix (sitemap filling
CANDIDATE_CAP_PER_ORG must not short-circuit the homepage phase).
"""
from __future__ import annotations


def test_sitemap_html_report_page_gets_subpage_expanded():
    """TICK-007: sitemap lists /annual-report/ (HTML), that page
    contains a PDF link with mundane anchor text. Expected: the
    PDF surfaces (via subpage-expansion + TICK-001 relaxed filter)."""
    from lavandula.reports.discover import per_org_candidates

    # Sitemap at /sitemap.xml (single urlset — small-site format).
    jccsf_sitemap = b'''<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.org/about/financial-statements/annual-report/</loc></url>
  <url><loc>https://example.org/events/gala-2024</loc></url>
</urlset>'''

    # Landing page body — has a PDF link with mundane anchor text.
    landing_page = b'''<html><body>
      <h1>Annual Reports</h1>
      <a href="/wp-content/uploads/2026/01/ImpactReport.pdf">Download</a>
      <a href="/events/">Events</a>
    </body></html>'''

    def fake_fetch(url, kind):
        if kind == "sitemap":
            return jccsf_sitemap, "ok"
        if kind == "homepage":
            # Simulate WAF-blocked homepage — tiny response, zero links.
            return b"<html><body>Blocked</body></html>", "ok"
        if kind == "subpage":
            return landing_page, "ok"
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    urls = {c.url for c in candidates}
    # The key TICK-007 assertion: the PDF linked from the HTML
    # landing page (sitemap-surfaced) was discovered.
    assert any("ImpactReport.pdf" in u for u in urls), (
        f"TICK-007 regression: expected ImpactReport.pdf in candidates, got {urls}"
    )


def test_sitemap_subpage_expansion_runs_when_homepage_fails():
    """Even if homepage fetch returns 'forbidden' or similar,
    the sitemap-derived HTML candidates still get expanded."""
    from lavandula.reports.discover import per_org_candidates

    sitemap = b'''<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.org/reports/</loc></url>
</urlset>'''
    landing = b'''<html><body>
      <a href="/uploads/latest.pdf">Download</a>
    </body></html>'''

    def fake_fetch(url, kind):
        if kind == "sitemap":
            return sitemap, "ok"
        if kind == "homepage":
            # Return server_error — homepage is broken but sitemap isn't.
            return b"", "server_error"
        if kind == "subpage":
            return landing, "ok"
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    urls = {c.url for c in candidates}
    assert any("latest.pdf" in u for u in urls), (
        f"subpage expansion must still run when homepage failed, got {urls}"
    )


def test_c7dd01f_hotfix_sitemap_saturation_does_not_skip_homepage():
    """Regression test for the homepage-skip short-circuit bug.

    When the sitemap yields >CANDIDATE_CAP_PER_ORG URLs, the
    earlier implementation set cap_reached=True and returned
    without running the homepage phase. This is the unit-test-that-
    should-have-caught-the-bug-originally.
    """
    from lavandula.reports import config
    from lavandula.reports.discover import per_org_candidates

    # Build a sitemap with more URLs than the cap so we deliberately
    # saturate the sitemap phase.
    n = config.CANDIDATE_CAP_PER_ORG + 5
    urls_xml = "".join(
        f'<url><loc>https://example.org/reports/item-{i}.pdf</loc></url>'
        for i in range(n)
    )
    sitemap = (
        b'<?xml version="1.0"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + urls_xml.encode()
        + b'</urlset>'
    )

    homepage = (
        b'<html><body>'
        b'<a href="/annual-report">Annual Report</a>'
        b'</body></html>'
    )

    homepage_fetched = []
    def fake_fetch(url, kind):
        if kind == "sitemap":
            return sitemap, "ok"
        if kind == "homepage":
            homepage_fetched.append(url)
            return homepage, "ok"
        return b"", "not_found"

    per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    # The hotfix invariant: homepage MUST be fetched even when
    # the sitemap fills the cap.
    assert homepage_fetched, (
        "c7dd01f regression: homepage fetch was skipped because "
        "sitemap filled the candidate cap — this is the exact "
        "bug the hotfix is supposed to prevent."
    )
