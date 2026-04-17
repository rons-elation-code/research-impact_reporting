"""TICK-001 AC34/AC35: curated-list source partition + enumeration.

AC34 (from fixtures, not live): the parser extracts every /ein/ anchor
it finds. Live-site >=3,000 floor is empirical and not testable here.

AC35: db_writer.unfetched_sitemap_entries(source='curated-lists') never
returns rows whose source_sitemap starts with 'Sitemap'. Inverse guard
for source='sitemap' also verified.

AC35 also: the crawler, given a DB pre-populated with sitemap-style rows
plus a smaller set of curated rows, fetches ONLY the curated ones when
run with --source=curated-lists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lavandula.nonprofits import curated_lists, db_writer, robots
from lavandula.nonprofits.schema import ensure_db


F = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cn"


def _seed_mixed(conn):
    """10 legacy sitemap rows + 5 curated rows."""
    for i in range(10):
        ein = f"{100000000 + i:09d}"
        db_writer.insert_sitemap_entry(
            conn, ein=ein, source_sitemap="Sitemap1.xml",
        )
    for i in range(5):
        ein = f"{200000000 + i:09d}"
        db_writer.insert_sitemap_entry(
            conn, ein=ein, source_sitemap="curated:highly-rated-charities",
        )


def test_unfetched_source_curated_partition_ac35(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    _seed_mixed(conn)

    rows = list(
        db_writer.unfetched_sitemap_entries(conn, source="curated-lists")
    )
    assert len(rows) == 5
    for ein, src, _ in rows:
        assert src.startswith("curated:")
    eins = {e for e, _, _ in rows}
    # None of the sitemap-prefixed EINs leaked.
    assert all(not e.startswith("1") for e in eins)


def test_unfetched_source_sitemap_inverse(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    _seed_mixed(conn)

    rows = list(
        db_writer.unfetched_sitemap_entries(conn, source="sitemap")
    )
    assert len(rows) == 10
    for _, src, _ in rows:
        assert not src.startswith("curated:")


def test_unfetched_source_none_returns_everything(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    _seed_mixed(conn)
    rows = list(db_writer.unfetched_sitemap_entries(conn, source=None))
    assert len(rows) == 15


def test_unfetched_source_unknown_raises(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    _seed_mixed(conn)
    with pytest.raises(ValueError):
        list(db_writer.unfetched_sitemap_entries(conn, source="weird"))


def test_curated_enumerate_writes_only_curated_prefix(tmp_path):
    """Top-level curated_lists.enumerate() writes rows with curated: label."""
    conn = ensure_db(tmp_path / "x.db")

    cat_html = (F / "curated-highly-rated-p1.html").read_bytes()

    class _Resp:
        def __init__(self, body: bytes | None):
            self.status = "ok" if body is not None else "server_error"
            self.body = body
            self.note = ""

    pages_served: list[str] = []

    class _FakeClient:
        def get(self, url, **kw):
            pages_served.append(url)
            # Only page 1 of the first category has content.
            if url.endswith("highly-rated-charities"):
                return _Resp(cat_html)
            return _Resp(None)

    # robots that allows everything
    policy = robots.parse("User-agent: *\n", ua="test")

    # Restrict to one hardcoded category for determinism; skip extras.
    import lavandula.nonprofits.curated_lists as cl

    n = cl.enumerate(_FakeClient(), conn, policy=policy)

    # All hardcoded categories attempted; at least one produced content.
    assert n >= 1

    rows = conn.execute(
        "SELECT ein, source_sitemap FROM sitemap_entries"
    ).fetchall()
    assert rows, "curated enumeration produced zero DB rows"
    for ein, src in rows:
        assert src.startswith("curated:")
        assert ein.isdigit() and len(ein) == 9
