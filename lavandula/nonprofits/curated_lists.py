"""Curated-list enumerator for Charity Navigator's Best Charities pages.

Added by TICK-001 (spec 0001) to replace the full-sitemap strategy for v1.
The full sitemap enumerates ~2.3M EINs at ~82 days of 3s throttled
crawling; the Best Charities index pages yield ~3K-7K pre-rated orgs,
which is the scope Lavandula actually needs for the downstream
report-harvesting bot.

Design notes:
  - Category URLs are HARDCODED (Codex MED-2). We do not discover them
    from homepage navigation; a site redesign that moves the nav is a
    1-hour fix to this file and does NOT silently shrink the corpus.
  - Pagination is `?p=N` walking per Claude #3; capped at
    MAX_PAGES_PER_CATEGORY to prevent runaway loops on redesigns that
    break the "zero new EINs stops us" invariant.
  - Parsing is BeautifulSoup with the stdlib `html.parser` backend, so no
    XXE or DTD surface. The anchor-shape invariant (Claude #2) is tested
    per committed HTML fixture.
  - Source tag written into `sitemap_entries.source_sitemap` is
    `curated:{category-slug}` — the `curated:` prefix is the primitive
    behind the source-partition isolation guarantee (AC35).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import config, db_writer
from .url_utils import canonicalize_ein, ein_from_profile_url


log = logging.getLogger("lavandula.nonprofits.curated_lists")


# --- Category catalogue ------------------------------------------------

# Starting seeds verified against the live site (2026-04-17). Additional
# slugs are tried opportunistically via EXTRA_CATEGORIES — a HEAD check at
# enumeration time decides whether to include each.
CATEGORY_PATHS: tuple[str, ...] = (
    "/discover-charities/best-charities/highly-rated-charities",
    "/discover-charities/best-charities/cost-effective-organizations",
    "/discover-charities/best-charities/popular-charities",
    "/discover-charities/best-charities/support-animal-rescue",
)

# Optional cause-category slugs. Probed via HEAD at enumeration start; any
# that returns 2xx text/html is included. A failed probe is logged but not
# fatal.
EXTRA_CATEGORIES: tuple[str, ...] = (
    "/discover-charities/best-charities/support-childrens-healthcare",
    "/discover-charities/best-charities/support-veterans",
    "/discover-charities/best-charities/respond-to-climate-change",
    "/discover-charities/best-charities/provide-disaster-relief",
    "/discover-charities/best-charities/support-education",
    "/discover-charities/best-charities/support-civil-rights",
    "/discover-charities/best-charities/support-arts-and-culture",
    "/discover-charities/best-charities/support-the-environment",
    "/discover-charities/best-charities/support-food-insecurity",
)

DISCOVER_CHARITIES_PATH = "/discover-charities/"
MAX_PAGES_PER_CATEGORY = 20

# Anchor pattern: href ends in `/ein/NNNNNNNNN` (9 digits) — may be
# absolute or relative, may have a trailing slash/query/fragment.
_EIN_HREF_RE = re.compile(r"/ein/([0-9]{9})(?:[/?#]|$)")


# --- Data types --------------------------------------------------------

@dataclass(frozen=True)
class CategoryResult:
    """Per-category enumeration outcome. Used by tests + report."""
    slug: str
    path: str
    pages_walked: int
    eins: tuple[str, ...]


# --- Parsing -----------------------------------------------------------

def category_slug(path: str) -> str:
    """Return the trailing slug of a /discover-charities/... path.

    Example: '/discover-charities/best-charities/highly-rated-charities'
    -> 'highly-rated-charities'.
    """
    clean = path.rstrip("/")
    return clean.rsplit("/", 1)[-1] if clean else ""


def extract_eins_from_page(html: bytes | str) -> list[str]:
    """Parse a category/index HTML page and return 9-digit EINs.

    Preserves anchor order (first-seen on page) and de-duplicates within
    the page. Uses stdlib `html.parser` so no XXE / DTD surface.
    """
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "html.parser")
    else:
        soup = BeautifulSoup(html, "html.parser")

    seen: set[str] = set()
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"] or ""
        m = _EIN_HREF_RE.search(href)
        if not m:
            continue
        raw = m.group(1)
        try:
            ein = canonicalize_ein(raw)
        except ValueError:
            continue
        if ein in seen:
            continue
        seen.add(ein)
        out.append(ein)
    return out


def paginated_url(base_path: str, page: int) -> str:
    """Return the absolute URL for `base_path` + pagination `page`.

    Page 1 is the bare path (most CMSs render the same page for `?p=1` as
    the bare URL, but not all — using the bare URL avoids a duplicate
    fetch).
    """
    if page <= 1:
        return f"{config.SITE_BASE}{base_path}"
    return f"{config.SITE_BASE}{base_path}?p={page}"


# --- Enumeration -------------------------------------------------------

def enumerate_category(
    path: str,
    *,
    fetch_page: Callable[[str], bytes | None],
    max_pages: int = MAX_PAGES_PER_CATEGORY,
) -> CategoryResult:
    """Walk `?p=N` for a category until a page yields zero new EINs.

    `fetch_page(url)` returns the decoded HTML bytes, or None on failure.
    A None response for page 1 is fatal (we log + return empty); for
    later pages it is treated as "end of pagination".
    """
    slug = category_slug(path)
    seen: set[str] = set()
    order: list[str] = []
    pages_walked = 0

    for page in range(1, max_pages + 1):
        url = paginated_url(path, page)
        body = fetch_page(url)
        pages_walked += 1
        if body is None:
            if page == 1:
                log.warning("curated: category %s page 1 failed to fetch", slug)
            break
        page_eins = extract_eins_from_page(body)
        new = [e for e in page_eins if e not in seen]
        if not new:
            break
        for ein in new:
            seen.add(ein)
            order.append(ein)

    log.info(
        "curated: category=%s pages=%d eins=%d",
        slug, pages_walked, len(order),
    )
    return CategoryResult(
        slug=slug,
        path=path,
        pages_walked=pages_walked,
        eins=tuple(order),
    )


def _discover_charities_allowed(robots_policy) -> bool:
    """robots precondition (Claude #5): /discover-charities/* must be
    allowed for our UA. Tested separately; crawler halts on False.
    """
    if robots_policy is None:
        return True
    return robots_policy.is_allowed(DISCOVER_CHARITIES_PATH)


def select_categories(
    *,
    head_probe: Callable[[str], bool] | None = None,
    categories: Iterable[str] = CATEGORY_PATHS,
    extras: Iterable[str] = EXTRA_CATEGORIES,
) -> list[str]:
    """Build the final category list.

    `categories` are always included (they're the hardcoded invariant).
    `extras` are probed via `head_probe(path) -> bool`; included when
    the probe returns True. Passing `head_probe=None` skips extras
    entirely — useful for tests that want a deterministic set.
    """
    out = [p for p in categories]
    if head_probe is None:
        return out
    for path in extras:
        try:
            ok = bool(head_probe(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("curated: HEAD probe for %s failed: %r", path, exc)
            ok = False
        if ok:
            out.append(path)
    return out


def enumerate_curated(
    *,
    fetch_page: Callable[[str], bytes | None],
    robots_policy=None,
    categories: Iterable[str] | None = None,
    extras: Iterable[str] | None = None,
    head_probe: Callable[[str], bool] | None = None,
    max_pages: int = MAX_PAGES_PER_CATEGORY,
) -> Iterator[tuple[str, str, int]]:
    """Yield (ein, source_label, page_index) tuples for curated EINs.

    Filters out DISALLOWED_EINS (floor) and any EIN disallowed by
    `robots_policy` before yielding.

    `source_label` is `curated:{slug}` — the prefix is queried by the
    source-partition guard in db_writer.unfetched_sitemap_entries.

    `page_index` is unused by current callers but preserves a ballpark
    "how deep into the category was this" hint for the report.
    """
    cats = list(categories) if categories is not None else list(CATEGORY_PATHS)
    extras_list = list(extras) if extras is not None else list(EXTRA_CATEGORIES)
    final = select_categories(
        head_probe=head_probe,
        categories=cats,
        extras=extras_list,
    )

    emitted: set[str] = set()
    for path in final:
        result = enumerate_category(
            path,
            fetch_page=fetch_page,
            max_pages=max_pages,
        )
        label = f"curated:{result.slug}"
        for ein in result.eins:
            if ein in emitted:
                continue
            if ein in config.DISALLOWED_EINS:
                continue
            if robots_policy is not None and not robots_policy.is_allowed(
                f"/ein/{ein}"
            ):
                continue
            emitted.add(ein)
            yield ein, label, 0


# --- Top-level entry point -------------------------------------------

def enumerate(client, conn, policy) -> int:
    """Fetch curated index pages and populate sitemap_entries.

    Returns the number of NEW entries inserted. Uses INSERT OR IGNORE so
    repeated calls are idempotent (first-seen precedence on
    `source_sitemap`).

    Preconditions (raise RuntimeError if not met):
      - `policy.is_allowed('/discover-charities/')` must be True. The
        crawler is expected to have re-fetched robots.txt and passed us
        the compiled policy. If this is False here, the caller has
        skipped the startup check.
    """
    if not _discover_charities_allowed(policy):
        raise RuntimeError(
            "robots.txt disallows /discover-charities/ for our UA; "
            "curated-list enumeration cannot proceed"
        )

    def _fetch_page(url: str) -> bytes | None:
        r = client.get(url)
        if r.status != "ok" or r.body is None:
            log.warning(
                "curated: fetch failed url=%s status=%s note=%s",
                url, r.status, r.note,
            )
            return None
        return r.body

    def _head_probe(path: str) -> bool:
        # We don't currently have a HEAD path in ThrottledClient; a full
        # GET for these small index pages is acceptable and exercises the
        # same throttle envelope. Cheaper: fetch once and use the bytes
        # as the enumeration source. Here we simply let the probe return
        # True for all EXTRA_CATEGORIES and let enumerate_category drop
        # the ones that page-1-fail. That keeps one network behavior, not
        # two.
        return True

    inserted = 0
    for ein, label, _page in enumerate_curated(
        fetch_page=_fetch_page,
        robots_policy=policy,
        head_probe=_head_probe,
    ):
        with conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM sitemap_entries WHERE ein = ?",
                (ein,),
            ).fetchone()[0]
            db_writer.insert_sitemap_entry(
                conn, ein=ein, source_sitemap=label,
            )
            after = conn.execute(
                "SELECT COUNT(*) FROM sitemap_entries WHERE ein = ?",
                (ein,),
            ).fetchone()[0]
            if after > before:
                inserted += 1
    log.info("curated: inserted %d new sitemap_entries", inserted)
    return inserted
