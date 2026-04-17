"""TICK-001 AC35: --source dispatch isolation inside the crawler.

Exercises `crawler._enumerate_if_empty` directly:
  - source='curated-lists' never fetches the XML sitemap index.
  - source='sitemap' never invokes the curated enumerator.
  - A DB pre-populated with legacy sitemap rows does not short-circuit
    the curated enumerator (its "is empty" check is source-scoped).
"""
from __future__ import annotations

from pathlib import Path

from lavandula.nonprofits import config, crawler, db_writer, robots
from lavandula.nonprofits.schema import ensure_db


F = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cn"


class _Resp:
    def __init__(self, body=None, status="ok"):
        self.body = body
        self.status = status
        self.note = ""


class _ScriptedClient:
    """Records every URL requested via .get()."""

    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        if url in self.responses:
            return _Resp(self.responses[url])
        return _Resp(None, status="server_error")


def test_curated_source_never_fetches_sitemap_index(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    cat_html = (F / "curated-animal-rescue-p1.html").read_bytes()

    # Provide only the single animal-rescue page as a hit; other
    # categories 404.
    responses = {
        f"{config.SITE_BASE}/discover-charities/best-charities/support-animal-rescue": cat_html,
    }
    client = _ScriptedClient(responses)
    policy = robots.parse("User-agent: *\n", ua="test")

    n = crawler._enumerate_if_empty(client, conn, policy, source=crawler.SOURCE_CURATED)
    assert n >= 1

    # AC35 core assertion: the sitemap index URL was NEVER requested.
    assert config.SITEMAP_INDEX_URL not in client.calls
    # All fetches are under /discover-charities/.
    for url in client.calls:
        assert "/discover-charities/" in url

    # Rows in DB are exclusively curated-prefixed.
    rows = conn.execute("SELECT source_sitemap FROM sitemap_entries").fetchall()
    assert rows
    for (src,) in rows:
        assert src.startswith("curated:")


def test_sitemap_source_enumerate_if_empty_routes_to_legacy(tmp_path):
    """source='sitemap' must fetch the XML sitemap index, NOT curated pages."""
    conn = ensure_db(tmp_path / "x.db")

    # Build a minimal valid sitemap index + one child sitemap.
    index_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://www.charitynavigator.org/Sitemap1.xml</loc>
  </sitemap>
</sitemapindex>
"""
    child_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.charitynavigator.org/ein/530196605</loc></url>
  <url><loc>https://www.charitynavigator.org/ein/131760110</loc></url>
</urlset>
"""
    responses = {
        config.SITEMAP_INDEX_URL: index_xml,
        "https://www.charitynavigator.org/Sitemap1.xml": child_xml,
    }
    client = _ScriptedClient(responses)
    policy = robots.parse("User-agent: *\n", ua="test")

    n = crawler._enumerate_if_empty(client, conn, policy, source=crawler.SOURCE_SITEMAP)
    assert n == 2
    # No curated URL was ever fetched in sitemap mode.
    assert all("/discover-charities/" not in u for u in client.calls)
    rows = conn.execute("SELECT source_sitemap FROM sitemap_entries").fetchall()
    for (src,) in rows:
        assert not src.startswith("curated:")


def test_curated_skips_enumeration_if_curated_rows_exist(tmp_path):
    """source='curated-lists' with existing curated rows does NOT re-enumerate."""
    conn = ensure_db(tmp_path / "x.db")
    db_writer.insert_sitemap_entry(
        conn, ein="530196605", source_sitemap="curated:highly-rated-charities",
    )

    client = _ScriptedClient({})
    policy = robots.parse("User-agent: *\n", ua="test")
    n = crawler._enumerate_if_empty(client, conn, policy, source=crawler.SOURCE_CURATED)
    # Returns the existing-count short-circuit.
    assert n == 1
    # No network calls.
    assert client.calls == []


def test_curated_source_ignores_legacy_sitemap_rows_when_deciding_empty(tmp_path):
    """If only sitemap-prefixed rows exist, curated source is 'empty' and enumerates."""
    conn = ensure_db(tmp_path / "x.db")
    # Seed ONLY legacy sitemap rows.
    for i in range(3):
        db_writer.insert_sitemap_entry(
            conn, ein=f"{100000000 + i:09d}", source_sitemap="Sitemap1.xml",
        )

    cat_html = (F / "curated-animal-rescue-p1.html").read_bytes()
    responses = {
        f"{config.SITE_BASE}/discover-charities/best-charities/support-animal-rescue": cat_html,
    }
    client = _ScriptedClient(responses)
    policy = robots.parse("User-agent: *\n", ua="test")

    # Despite 3 rows in DB, curated mode treats itself as empty and
    # runs enumeration (legacy rows don't block us).
    n = crawler._enumerate_if_empty(client, conn, policy, source=crawler.SOURCE_CURATED)
    assert n >= 1

    # All fetches are curated-side.
    for url in client.calls:
        assert "/discover-charities/" in url or url == config.SITE_BASE + "/"
    # Sitemap index NOT fetched.
    assert config.SITEMAP_INDEX_URL not in client.calls
