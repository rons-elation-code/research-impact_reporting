"""AC8, AC9, AC10: sitemap XXE defense, enumeration, malformed filtering."""
from pathlib import Path

import pytest

from lavandula.nonprofits import sitemap


F = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cn"


def test_sitemap_index_enumerates_48():
    body = (F / "extra-index-48.xml").read_bytes()
    urls = sitemap.parse_sitemap_index(body)
    assert len(urls) == 48
    assert all(u.startswith("https://www.charitynavigator.org/") for u in urls)


def test_xxe_entity_not_resolved():
    body = (F / "xxe-sitemap.xml").read_bytes()
    # defusedxml should raise on DTDs; the fallback lxml with
    # resolve_entities=False should parse-but-not-resolve.
    try:
        locs = sitemap.parse_child_sitemap(body)
    except sitemap.SitemapError:
        # defusedxml path — it raises on DTDs by default. Pass.
        return
    # lxml fallback path — assert no /etc/passwd content leaked.
    joined = " ".join(l.url for l in locs)
    assert "root:" not in joined
    # The safe EIN still extracted:
    eins = [sitemap.parse_child_sitemap(body) for _ in range(1)][0]
    valid = [loc for loc in locs if "530196605" in loc.url]
    assert len(valid) >= 1


def test_xxe_ssrf_does_not_fetch(monkeypatch):
    """Parsing must NOT trigger any network call for the SYSTEM entity."""
    import socket
    calls = []

    def _guard(*args, **kwargs):
        calls.append(args)
        raise AssertionError("XXE triggered outbound socket")

    monkeypatch.setattr(socket, "create_connection", _guard)
    body = (F / "xxe-ssrf-sitemap.xml").read_bytes()
    try:
        sitemap.parse_child_sitemap(body)
    except sitemap.SitemapError:
        pass
    assert calls == []


def test_malformed_xml_raises():
    body = (F / "malformed-sitemap.xml").read_bytes()
    with pytest.raises(sitemap.SitemapError):
        sitemap.parse_child_sitemap(body)


def test_duplicate_and_malformed_entries_filter():
    body = (F / "sitemap-with-duplicate-ein.xml").read_bytes()
    locs = sitemap.parse_child_sitemap(body)
    # Malformed entries (ABC12345, 12345678) are dropped; valid + duplicate
    # stays in the parser (dedup is caller's job via INSERT OR IGNORE).
    valid = [l for l in locs if "/ein/" in l.url]
    eins = [l.url.rsplit("/", 1)[-1] for l in valid]
    # No non-9-digit EINs should appear.
    for e in eins:
        assert e.isdigit() and len(e) == 9


def test_enumerate_filters_disallowed(tmp_path):
    from lavandula.nonprofits import robots
    robots_text = (F / "robots-simple.txt").read_text()
    policy = robots.parse(robots_text, ua="ua")
    index_body = (F / "extra-index-48.xml").read_bytes()
    child_body = (F / "sitemap-with-duplicate-ein.xml").read_bytes()

    def fetcher(url):
        return child_body

    out = list(sitemap.enumerate_sitemap_entries(
        index_xml=index_body,
        child_fetcher=fetcher,
        robots_policy=policy,
        sitemap_label_from_url=lambda u: "SitemapX.xml",
    ))
    eins = [e for e, _, _ in out]
    # Disallowed 863371262 must not appear.
    assert "863371262" not in eins
    # Valid EINs do.
    assert "530196605" in eins
