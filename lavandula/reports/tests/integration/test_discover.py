"""AC5 — one-hop subpage expansion."""
from __future__ import annotations

import pytest


HOMEPAGE = """
<html><body>
  <a href="/about/reports">Annual Reports</a>
  <a href="/news">News</a>
</body></html>
"""

SUBPAGE = """
<html><body>
  <a href="/reports/2024-annual.pdf">2024 Annual Report</a>
  <a href="/reports/2023-annual.pdf">2023 Annual Report</a>
</body></html>
"""


def test_ac5_subpage_expansion_adds_pdfs():
    """Given an /about/reports link on the homepage, the subpage's PDFs must become candidates."""
    from lavandula.reports.discover import per_org_candidates

    def fake_fetch(url, kind):
        if url.rstrip("/") == "https://example.org":
            return HOMEPAGE.encode(), "ok"
        if url == "https://example.org/about/reports":
            return SUBPAGE.encode(), "ok"
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org/",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    urls = {c.url for c in candidates}
    assert "https://example.org/reports/2024-annual.pdf" in urls
    assert "https://example.org/reports/2023-annual.pdf" in urls


def test_ac5_subpage_depth_cap():
    """More than MAX_SUBPAGES_PER_ORG subpage links from homepage → extra skipped."""
    from lavandula.reports.discover import per_org_candidates, MAX_SUBPAGES_PER_ORG
    # TICK-002 Fix 3: raised from 5 → 10.
    assert MAX_SUBPAGES_PER_ORG == 10

    subpages_visited = []
    homepage_links = "".join(
        f'<a href="/reports/{i}/index.html">Annual Report {i}</a>' for i in range(20)
    )
    homepage = f"<html><body>{homepage_links}</body></html>"

    def fake_fetch(url, kind):
        if url.rstrip("/") == "https://example.org":
            return homepage.encode(), "ok"
        if kind == "sitemap":
            # TICK-004: sitemap fetch now runs before homepage; return
            # 404-ish so sitemap phase contributes nothing and we
            # isolate the subpage cap behavior under test.
            return b"", "not_found"
        subpages_visited.append(url)
        return b"<html></html>", "ok"

    per_org_candidates(
        seed_url="https://example.org/",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    # Only MAX_SUBPAGES_PER_ORG subpage fetches should occur.
    assert len(subpages_visited) <= MAX_SUBPAGES_PER_ORG
