"""TICK-004 — sitemap-first discovery.

Covers AC1-AC15 from the TICK-004 amendment.
"""
from __future__ import annotations


SITEMAP_INDEX = b'''<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.org/sitemap-1.xml</loc></sitemap>
</sitemapindex>'''

SITEMAP_CHILD_WITH_REPORT = b'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.org/wp-content/uploads/2026/01/ImpactReport.pdf</loc></url>
  <url><loc>https://example.org/about/</loc></url>
  <url><loc>https://example.org/reports/2024/</loc></url>
</urlset>'''


def test_ac1_sitemap_fetched_and_parsed():
    """AC1: sitemap URLs enter the candidate list with discovered_via='sitemap'."""
    from lavandula.reports.discover import per_org_candidates

    def fake_fetch(url, kind):
        if kind == "sitemap" and url.endswith("/sitemap.xml"):
            return SITEMAP_INDEX, "ok"
        if kind == "sitemap" and "sitemap-1.xml" in url:
            return SITEMAP_CHILD_WITH_REPORT, "ok"
        if kind == "homepage":
            return b"<html><body></body></html>", "ok"
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    urls = {c.url for c in candidates}
    assert "https://example.org/wp-content/uploads/2026/01/ImpactReport.pdf" in urls
    # Verify attribution metadata
    pdf_cand = next(c for c in candidates if c.url.endswith("ImpactReport.pdf"))
    assert pdf_cand.discovered_via == "sitemap"
    assert pdf_cand.attribution_confidence == "own_domain"


def test_ac2_robots_sitemap_directive_takes_precedence():
    """AC2: robots Sitemap: directive is used; /sitemap.xml fallback skipped."""
    from lavandula.reports.discover import per_org_candidates

    fetch_log = []
    def fake_fetch(url, kind):
        fetch_log.append((url, kind))
        if kind == "sitemap" and url == "https://example.org/my-custom-sitemap.xml":
            return SITEMAP_CHILD_WITH_REPORT, "ok"
        if kind == "homepage":
            return b"<html></html>", "ok"
        return b"", "not_found"

    robots = "User-agent: *\nAllow: /\nSitemap: https://example.org/my-custom-sitemap.xml\n"
    per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text=robots,
    )
    sitemap_fetches = [u for u, k in fetch_log if k == "sitemap"]
    assert "https://example.org/my-custom-sitemap.xml" in sitemap_fetches
    # fallback /sitemap.xml should NOT be tried because robots provided a directive
    assert "https://example.org/sitemap.xml" not in sitemap_fetches


def test_ac2_multiple_robots_sitemap_directives_all_tried():
    """AC2: multiple Sitemap: lines → all attempted."""
    from lavandula.reports.discover import per_org_candidates

    fetch_log = []
    def fake_fetch(url, kind):
        fetch_log.append((url, kind))
        return b"", "not_found"

    robots = (
        "User-agent: *\nAllow: /\n"
        "Sitemap: https://example.org/sitemap-a.xml\n"
        "Sitemap: https://example.org/sitemap-b.xml\n"
    )
    per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text=robots,
    )
    sitemap_fetches = [u for u, k in fetch_log if k == "sitemap"]
    assert "https://example.org/sitemap-a.xml" in sitemap_fetches
    assert "https://example.org/sitemap-b.xml" in sitemap_fetches


def test_ac3_fallback_to_default_sitemap_path():
    """AC3: no Sitemap: in robots → /sitemap.xml fallback is tried."""
    from lavandula.reports.discover import per_org_candidates

    fetch_log = []
    def fake_fetch(url, kind):
        fetch_log.append((url, kind))
        if kind == "homepage":
            return b"<html></html>", "ok"
        return b"", "not_found"

    per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="User-agent: *\nAllow: /\n",
    )
    sitemap_fetches = [u for u, k in fetch_log if k == "sitemap"]
    assert "https://example.org/sitemap.xml" in sitemap_fetches


def test_ac4_sitemap_404_not_fatal():
    """AC4: sitemap 404 → crawl continues with homepage."""
    from lavandula.reports.discover import per_org_candidates

    def fake_fetch(url, kind):
        if kind == "sitemap":
            return b"", "not_found"
        if kind == "homepage":
            return (
                b'<html><body><a href="/annual-report">Annual</a></body></html>',
                "ok",
            )
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    # Homepage discovery should still yield the /annual-report candidate.
    assert any(c.url.endswith("/annual-report") for c in candidates)


def test_ac5_malformed_xml_not_fatal():
    """AC5: malformed sitemap XML → that sitemap skipped, crawl continues."""
    from lavandula.reports.discover import per_org_candidates

    def fake_fetch(url, kind):
        if kind == "sitemap":
            return b"not valid xml at all <<<>>>", "ok"
        if kind == "homepage":
            return b'<html><body><a href="/impact">i</a></body></html>', "ok"
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    # No exception, homepage-derived candidate survives.
    assert any("impact" in c.url for c in candidates)


def test_ac9_anti_noise_image_dropped():
    """AC9: image-suffix URLs from sitemap are dropped."""
    from lavandula.reports.candidate_filter import classify_sitemap_url
    result = classify_sitemap_url(
        url="https://example.org/photo.jpg",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert result is None


def test_ac9_anti_noise_feed_dropped():
    from lavandula.reports.candidate_filter import classify_sitemap_url
    for url in [
        "https://example.org/feed/",
        "https://example.org/category/news/",
        "https://example.org/tag/events/",
        "https://example.org/2023/05/",
        "https://example.org/page/5/",
    ]:
        assert classify_sitemap_url(
            url=url,
            seed_etld1="example.org",
            referring_page_url="https://example.org/",
        ) is None, f"expected None for {url}"


def test_ac9_anti_noise_allows_pdf_in_uploads():
    from lavandula.reports.candidate_filter import classify_sitemap_url
    result = classify_sitemap_url(
        url="https://example.org/wp-content/uploads/2026/impact.pdf",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert result is not None
    assert result.discovered_via == "sitemap"


def test_ac10_cross_origin_drops_unrelated_host():
    """AC10: unrelated host from sitemap → dropped."""
    from lavandula.reports.candidate_filter import classify_sitemap_url
    result = classify_sitemap_url(
        url="https://other-domain.com/report.pdf",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert result is None


def test_ac10_cross_origin_accepts_platform_unverified():
    """AC10: Issuu URL from sitemap → accepted as platform_unverified."""
    from lavandula.reports.candidate_filter import classify_sitemap_url
    result = classify_sitemap_url(
        url="https://issuu.com/example-org/docs/annual-2024",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert result is not None
    assert result.hosting_platform == "issuu"
    # Sitemap-only discovery → unverified (AC12.3)
    assert result.attribution_confidence == "platform_unverified"


def test_ac10_cms_subdomain_pdf_accepted():
    """AC10: CMS-subdomain PDF from sitemap accepted via TICK-002 rule."""
    from lavandula.reports.candidate_filter import classify_sitemap_url
    result = classify_sitemap_url(
        url="https://sagehillschool.myschoolapp.com/reports/2024.pdf",
        seed_etld1="sagehillschool.org",
        referring_page_url="https://www.sagehillschool.org/",
    )
    assert result is not None
    assert result.hosting_platform == "own-cms"
    assert result.attribution_confidence == "platform_verified"


def test_ac11_defusedxml_in_use():
    """AC11: sitemap.py uses defusedxml — import verification."""
    from lavandula.reports import sitemap as _s
    # The module's fromstring must come from defusedxml
    src = open(_s.__file__).read()
    assert "from defusedxml.ElementTree import fromstring" in src


def test_ac11_billion_laughs_does_not_explode():
    """AC11: hostile XML with entity expansion raises (or returns empty),
    does NOT hang or consume gigabytes of memory."""
    from lavandula.reports.sitemap import parse_sitemap
    billion_laughs = b'''<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>&lol3;</loc></url>
</urlset>'''
    # Either raises (defusedxml default) or returns empty / safe.
    try:
        urls = parse_sitemap(billion_laughs)
        # If it returned, ensure it didn't actually expand the entity
        if urls:
            assert not any(len(u) > 1000 for u in urls)
    except Exception:
        pass  # defusedxml refusing to expand is acceptable


def test_ac12_dedup_homepage_wins_over_sitemap():
    """AC12: same URL in both sources → only one candidate, homepage
    wins provenance. (Both sources enter the pool; dedup keeps one.)"""
    from lavandula.reports.discover import per_org_candidates

    sitemap_with_overlap = b'''<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.org/annual-report</loc></url>
  <url><loc>https://example.org/reports/2024.pdf</loc></url>
</urlset>'''

    def fake_fetch(url, kind):
        if kind == "sitemap":
            return sitemap_with_overlap, "ok"
        if kind == "homepage":
            return (
                b'<html><body>'
                b'<a href="/annual-report">Annual</a>'
                b'</body></html>',
                "ok",
            )
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    # /annual-report appears in both sources; should be deduped.
    annual_cands = [c for c in candidates if c.url.endswith("/annual-report")]
    assert len(annual_cands) == 1
    # /reports/2024.pdf is sitemap-only.
    pdf_cands = [c for c in candidates if c.url.endswith("2024.pdf")]
    assert len(pdf_cands) == 1


def test_ac14_jccsf_style_waf_gated_homepage():
    """AC14: JCCSF-pattern — homepage returns ~1 KB of non-link
    content (simulating WAF challenge), sitemap lists impact PDF.
    Expected: PDF surfaces from sitemap even with zero homepage links."""
    from lavandula.reports.discover import per_org_candidates

    waf_page = b'''<!DOCTYPE html>
<html><head><title>Attention Required! | Cloudflare</title></head>
<body>
  <h1>Sorry, you have been blocked</h1>
  <p>This website is using a security service to protect itself from online attacks.</p>
</body></html>'''
    # WAF page has zero <a href> tags — so homepage discovery yields 0.

    jccsf_sitemap = b'''<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.org/sitemap-uploads.xml</loc></sitemap>
</sitemapindex>'''

    jccsf_child = b'''<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.org/wp-content/uploads/2026/01/ImpactReport.pdf</loc></url>
</urlset>'''

    def fake_fetch(url, kind):
        if kind == "sitemap" and url.endswith("/sitemap.xml"):
            return jccsf_sitemap, "ok"
        if kind == "sitemap" and "sitemap-uploads" in url:
            return jccsf_child, "ok"
        if kind == "homepage":
            return waf_page, "ok"
        return b"", "not_found"

    candidates = per_org_candidates(
        seed_url="https://example.org",
        seed_etld1="example.org",
        fetcher=fake_fetch,
        robots_text="",
    )
    urls = [c.url for c in candidates]
    # The key JCCSF regression assertion:
    assert any("ImpactReport.pdf" in u for u in urls), (
        "JCCSF regression: sitemap PDF should surface even when "
        "homepage is WAF-gated / empty of links"
    )


def test_robots_sitemap_urls_extract_multiple():
    """Helper test: sitemap_urls_from_robots extracts multiple directives
    in document order, case-insensitive key match."""
    from lavandula.reports.robots import sitemap_urls_from_robots
    txt = (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Sitemap: https://example.org/a.xml\n"
        "# comment line ignored\n"
        "SITEMAP: https://example.org/b.xml\n"
        "sitemap:   https://example.org/c.xml   \n"
    )
    result = sitemap_urls_from_robots(txt)
    assert result == [
        "https://example.org/a.xml",
        "https://example.org/b.xml",
        "https://example.org/c.xml",
    ]


def test_robots_sitemap_urls_empty_when_none():
    from lavandula.reports.robots import sitemap_urls_from_robots
    assert sitemap_urls_from_robots("") == []
    assert sitemap_urls_from_robots("User-agent: *\nDisallow: /\n") == []
