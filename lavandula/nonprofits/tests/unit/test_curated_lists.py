"""TICK-001: curated-list enumerator tests.

Covers:
  - AC34 (partial): per-fixture EIN counts on the curated index-page
    anchor parser. The live-site >=3,000 threshold is an empirical
    gate, not testable from fixtures — covered by the validation plan.
  - Pagination cap: category with infinite ?p=N pagination stops at
    MAX_PAGES_PER_CATEGORY.
  - DISALLOWED_EINS filter applies to curated source.
  - Robots precondition: /discover-charities/* allowed check drives
    RuntimeError when called with a policy that disallows.
  - category_slug helper extracts the trailing slug.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lavandula.nonprofits import config, curated_lists, robots


F = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cn"


# --- Anchor parser ------------------------------------------------------

def test_extract_eins_fixture_page1():
    html = (F / "curated-highly-rated-p1.html").read_bytes()
    eins = curated_lists.extract_eins_from_page(html)
    # Duplicates on the same page collapse; order preserved.
    assert eins == [
        "530196605",
        "131760110",
        "131644147",
        "530242652",
        "135562976",
        "136161455",
        "042103561",
    ]


def test_extract_eins_fixture_page2():
    html = (F / "curated-highly-rated-p2.html").read_bytes()
    eins = curated_lists.extract_eins_from_page(html)
    # Includes 530242652 (duplicate across pages) and the disallowed
    # 863371262 — filtering happens at the enumerator layer, not the
    # parser.
    assert "863371262" in eins
    assert eins.count("530242652") == 1


def test_extract_eins_rejects_non_nine_digit_anchors():
    html = (F / "curated-animal-rescue-p1.html").read_bytes()
    eins = curated_lists.extract_eins_from_page(html)
    # /ein/ABC123456 and /ein/12345678 are not 9-digit ASCII, rejected.
    for e in eins:
        assert e.isdigit() and len(e) == 9
    assert "411704734" in eins  # Best Friends
    assert "521106406" in eins  # Jane Goodall


def test_extract_eins_unwraps_absolute_urls_with_query():
    # Page 1 has https://www.charitynavigator.org/ein/135562976?utm_source=best
    html = (F / "curated-highly-rated-p1.html").read_bytes()
    eins = curated_lists.extract_eins_from_page(html)
    assert "135562976" in eins


# --- category_slug ------------------------------------------------------

def test_category_slug():
    assert (
        curated_lists.category_slug(
            "/discover-charities/best-charities/highly-rated-charities"
        )
        == "highly-rated-charities"
    )
    assert (
        curated_lists.category_slug(
            "/discover-charities/best-charities/support-animal-rescue/"
        )
        == "support-animal-rescue"
    )
    assert curated_lists.category_slug("") == ""


def test_paginated_url_page_one_is_bare():
    assert (
        curated_lists.paginated_url("/discover-charities/foo", 1)
        == f"{config.SITE_BASE}/discover-charities/foo"
    )
    assert (
        curated_lists.paginated_url("/discover-charities/foo", 3)
        == f"{config.SITE_BASE}/discover-charities/foo?p=3"
    )


# --- Pagination + enumerate_category ------------------------------------

def test_enumerate_category_walks_until_no_new_eins():
    """Stops when a page has no new EINs vs. the running seen-set."""
    pages = {
        f"{config.SITE_BASE}/cat": (F / "curated-highly-rated-p1.html").read_bytes(),
        f"{config.SITE_BASE}/cat?p=2": (F / "curated-highly-rated-p2.html").read_bytes(),
        # Page 3 is an empty page (should stop us).
        f"{config.SITE_BASE}/cat?p=3": b"<html><body>No more charities.</body></html>",
    }

    def fetch(url):
        return pages.get(url)

    result = curated_lists.enumerate_category("/cat", fetch_page=fetch)
    # Walked p1, p2, p3 (which yielded zero new and terminated).
    assert result.pages_walked == 3
    # All non-dup EINs across p1 + p2 present, order preserved.
    assert result.eins[:7] == (
        "530196605", "131760110", "131644147", "530242652",
        "135562976", "136161455", "042103561",
    )
    # Disallowed 863371262 is still in the parser output here (filter
    # is enumerator-level, not parser-level). This is intentional.
    assert "863371262" in result.eins


def test_enumerate_category_honors_pagination_cap():
    """Pagination cap (spec TICK-001 Claude #3): infinite-pagination
    site responds with endless unique EINs; we stop at max_pages."""

    def unique_page(url: str) -> bytes:
        # Extract the page number from ?p=N; fabricate 9-digit EINs that
        # never repeat across pages.
        import re as _re
        m = _re.search(r"[?&]p=(\d+)", url)
        page = int(m.group(1)) if m else 1
        base = 100_000_000 + page * 10
        anchors = "".join(
            f'<a href="/ein/{base + i:09d}">org</a>' for i in range(5)
        )
        return f"<html><body>{anchors}</body></html>".encode()

    result = curated_lists.enumerate_category(
        "/cat", fetch_page=unique_page, max_pages=4,
    )
    assert result.pages_walked == 4
    # 4 pages * 5 EINs/page = 20 unique EINs.
    assert len(result.eins) == 20


def test_enumerate_category_page_one_fetch_failure_returns_empty():
    """If page 1 fails, we stop immediately; pages_walked == 1."""

    def fetch(url):
        return None

    result = curated_lists.enumerate_category("/cat", fetch_page=fetch)
    assert result.pages_walked == 1
    assert result.eins == ()


# --- enumerate_curated (top layer) --------------------------------------

def test_enumerate_curated_filters_disallowed_and_dedups_across_categories():
    cat1 = (F / "curated-highly-rated-p1.html").read_bytes()
    cat2 = (F / "curated-highly-rated-p2.html").read_bytes()
    cat3 = (F / "curated-animal-rescue-p1.html").read_bytes()

    # Plan: category A emits p1 + p2 (p3 empty ends pagination); category B
    # emits its single page.
    pages = {
        f"{config.SITE_BASE}/discover-charities/best-charities/highly-rated-charities": cat1,
        f"{config.SITE_BASE}/discover-charities/best-charities/highly-rated-charities?p=2": cat2,
        f"{config.SITE_BASE}/discover-charities/best-charities/highly-rated-charities?p=3": b"<html></html>",
        f"{config.SITE_BASE}/discover-charities/best-charities/support-animal-rescue": cat3,
    }

    def fetch(url):
        return pages.get(url, b"<html></html>")

    # Trivial robots policy that allows everything.
    policy = robots.parse("User-agent: *\n", ua="test")

    out = list(
        curated_lists.enumerate_curated(
            fetch_page=fetch,
            robots_policy=policy,
            categories=(
                "/discover-charities/best-charities/highly-rated-charities",
                "/discover-charities/best-charities/support-animal-rescue",
            ),
            extras=(),
            head_probe=None,
        )
    )
    eins = [e for e, _, _ in out]
    labels = {src for _, src, _ in out}

    # Robots-disallowed EIN is filtered at the enumerator layer.
    assert "863371262" not in eins
    # Valid EINs present.
    assert "530196605" in eins  # Red Cross (category 1)
    assert "411704734" in eins  # Best Friends (shared across both)
    # Deduplication across categories: Best Friends appears in both but
    # emits exactly once.
    assert eins.count("411704734") == 1
    # Labels namespaced with `curated:` prefix.
    assert "curated:highly-rated-charities" in labels
    assert "curated:support-animal-rescue" in labels


def test_enumerate_curated_respects_robots_policy():
    cat1 = (F / "curated-highly-rated-p1.html").read_bytes()

    pages = {
        f"{config.SITE_BASE}/cat": cat1,
        f"{config.SITE_BASE}/cat?p=2": b"<html></html>",
    }

    def fetch(url):
        return pages.get(url, b"<html></html>")

    # Policy that disallows one specific EIN path.
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /ein/131760110\n"
    )
    policy = robots.parse(robots_txt, ua="test")
    out = list(
        curated_lists.enumerate_curated(
            fetch_page=fetch,
            robots_policy=policy,
            categories=("/cat",),
            extras=(),
            head_probe=None,
        )
    )
    eins = [e for e, _, _ in out]
    assert "131760110" not in eins
    assert "530196605" in eins


# --- Robots precondition -----------------------------------------------

def test_enumerate_raises_if_discover_charities_disallowed():
    """Claude #5: /discover-charities/* disallow -> RuntimeError."""
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /discover-charities/\n"
    )
    policy = robots.parse(robots_txt, ua="test")

    class _FakeClient:
        def get(self, url, **kw):
            raise AssertionError("should not fetch if robots precondition fails")

    class _FakeConn:
        pass

    with pytest.raises(RuntimeError, match="robots.txt disallows"):
        curated_lists.enumerate(_FakeClient(), _FakeConn(), policy)


# --- select_categories -------------------------------------------------

def test_select_categories_no_probe_returns_only_hardcoded():
    base = curated_lists.select_categories(head_probe=None)
    assert base == list(curated_lists.CATEGORY_PATHS)


def test_select_categories_probe_decides_extras():
    probed: list[str] = []

    def probe(path: str) -> bool:
        probed.append(path)
        return "support-education" in path

    out = curated_lists.select_categories(
        head_probe=probe,
        categories=("/discover-charities/best-charities/highly-rated-charities",),
        extras=(
            "/discover-charities/best-charities/support-education",
            "/discover-charities/best-charities/unknown-slug",
        ),
    )
    assert out == [
        "/discover-charities/best-charities/highly-rated-charities",
        "/discover-charities/best-charities/support-education",
    ]
    # Both extras were probed even though one was rejected.
    assert len(probed) == 2


def test_select_categories_probe_exception_treated_as_false(caplog):
    def boom(path: str) -> bool:
        raise RuntimeError("head failed")

    out = curated_lists.select_categories(
        head_probe=boom,
        categories=("/keep",),
        extras=("/dropped",),
    )
    assert out == ["/keep"]
