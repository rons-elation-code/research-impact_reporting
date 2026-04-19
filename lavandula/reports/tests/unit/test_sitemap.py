"""AC8.1 — sitemap parse caps + XXE safety."""
from __future__ import annotations

import pytest


def test_ac8_1_sitemap_urls_per_org_cap():
    from lavandula.reports.sitemap import parse_sitemap, MAX_SITEMAP_URLS_PER_ORG
    urls = "".join(
        f"<url><loc>https://example.org/{i}</loc></url>"
        for i in range(MAX_SITEMAP_URLS_PER_ORG + 100)
    )
    xml = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()
    out = parse_sitemap(xml)
    assert len(out) <= MAX_SITEMAP_URLS_PER_ORG


def test_ac8_1_sitemap_depth_max_1_ignores_nested_indexes():
    from lavandula.reports.sitemap import parse_sitemap_index_recursive

    def fetcher(url):
        # child sitemap-index that contains a further sitemap-index — must NOT be walked.
        if url == "https://example.org/parent.xml":
            return (
                b'<?xml version="1.0"?>'
                b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<sitemap><loc>https://example.org/child-index.xml</loc></sitemap>"
                b"</sitemapindex>"
            )
        if url == "https://example.org/child-index.xml":
            return (
                b'<?xml version="1.0"?>'
                b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<sitemap><loc>https://example.org/grand.xml</loc></sitemap>"
                b"</sitemapindex>"
            )
        if url == "https://example.org/grand.xml":
            return (
                b'<?xml version="1.0"?>'
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>https://example.org/leaked</loc></url>"
                b"</urlset>"
            )
        return b""

    urls = parse_sitemap_index_recursive(
        "https://example.org/parent.xml", fetcher=fetcher
    )
    # grand.xml must NOT have been reached: depth > 1 is refused.
    assert all("leaked" not in u for u in urls)


def test_ac8_1_sitemap_index_cap():
    from lavandula.reports.sitemap import (
        parse_sitemap_index_recursive,
        MAX_SITEMAPS_PER_ORG,
    )

    child_locs = "".join(
        f"<sitemap><loc>https://example.org/s{i}.xml</loc></sitemap>"
        for i in range(MAX_SITEMAPS_PER_ORG + 5)
    )
    index_xml = (
        b'<?xml version="1.0"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + child_locs.encode()
        + b"</sitemapindex>"
    )
    urls_served = []

    def fetcher(url):
        if url == "https://example.org/index.xml":
            return index_xml
        i = int(url.split("/s")[-1].split(".xml")[0])
        urls_served.append(i)
        return (
            b'<?xml version="1.0"?>'
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + f"<url><loc>https://example.org/u{i}</loc></url>".encode()
            + b"</urlset>"
        )

    parse_sitemap_index_recursive(
        "https://example.org/index.xml", fetcher=fetcher
    )
    assert len(urls_served) <= MAX_SITEMAPS_PER_ORG


XXE_XML = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>&xxe;</loc></url>
</urlset>
"""


def test_ac8_1_xxe_safe():
    from lavandula.reports.sitemap import parse_sitemap
    # Must not resolve the entity; either empty loc or parser rejects entirely.
    out = parse_sitemap(XXE_XML)
    for u in out:
        assert "/etc/passwd" not in u
        assert "root:" not in u
