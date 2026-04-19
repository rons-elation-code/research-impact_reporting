"""Sitemap parsing (AC8.1) with defusedxml + per-spec caps.

- MAX_SITEMAP_URLS_PER_ORG (10_000): aggregate URLs emitted per org.
- MAX_SITEMAPS_PER_ORG (5): child sitemaps walked from a sitemap-index.
- MAX_SITEMAP_DEPTH (1): sitemap-index → child sitemaps; nested indexes NOT walked.
- defusedxml prevents XXE / entity expansion on adversarial input.
"""
from __future__ import annotations

from typing import Callable

from defusedxml.ElementTree import fromstring  # type: ignore

from . import config


MAX_SITEMAP_URLS_PER_ORG = config.MAX_SITEMAP_URLS_PER_ORG
MAX_SITEMAPS_PER_ORG = config.MAX_SITEMAPS_PER_ORG
MAX_SITEMAP_DEPTH = config.MAX_SITEMAP_DEPTH

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _find_all(root, local_name: str):
    """Find `<{ns}local_name>` descendants, tolerating missing NS."""
    # Ns-qualified.
    yield from root.iter(f"{_SITEMAP_NS}{local_name}")
    # Also handle no-namespace (some feeds omit xmlns).
    yield from root.iter(local_name)


def _locs(xml_bytes: bytes, *, want: str) -> list[str]:
    """Return `<loc>` values under the top-level container.

    `want` is 'url' for urlset or 'sitemap' for sitemapindex.
    """
    try:
        root = fromstring(xml_bytes)
    except Exception:
        return []
    urls: list[str] = []
    for elem in _find_all(root, want):
        loc = None
        for child in elem:
            tag = child.tag
            if tag in (f"{_SITEMAP_NS}loc", "loc"):
                loc = (child.text or "").strip()
                break
        if loc:
            urls.append(loc)
    return urls


def parse_sitemap(xml_bytes: bytes) -> list[str]:
    """Parse a urlset XML payload; return URLs capped at MAX_SITEMAP_URLS_PER_ORG."""
    urls = _locs(xml_bytes, want="url")
    return urls[:MAX_SITEMAP_URLS_PER_ORG]


def parse_sitemap_index_recursive(
    index_url: str,
    *,
    fetcher: Callable[[str], bytes | None],
) -> list[str]:
    """Walk a sitemap-index, applying AC8.1 caps.

    `fetcher(url) -> bytes|None` is how we retrieve child sitemaps —
    the caller wires this to the throttled HTTP client in production.
    Nested sitemap-indexes are NOT walked (depth cap = 1).
    """
    index_bytes = fetcher(index_url)
    if not index_bytes:
        return []
    child_locs = _locs(index_bytes, want="sitemap")[:MAX_SITEMAPS_PER_ORG]
    aggregated: list[str] = []
    for child_url in child_locs:
        body = fetcher(child_url)
        if not body:
            continue
        # Depth cap = 1: if the child is itself a sitemap-index, skip its
        # nested-index children silently (do not recurse).
        try:
            root = fromstring(body)
        except Exception:  # noqa: BLE001,S112  # nosec B112 — malformed child sitemap ignored; aggregate parse continues
            continue
        tag = root.tag
        if tag in (f"{_SITEMAP_NS}sitemapindex", "sitemapindex"):
            # Nested sitemap-index — spec cap is MAX_SITEMAP_DEPTH = 1; do not walk.
            continue
        for url in _locs(body, want="url"):
            aggregated.append(url)
            if len(aggregated) >= MAX_SITEMAP_URLS_PER_ORG:
                return aggregated
    return aggregated


__all__ = [
    "parse_sitemap",
    "parse_sitemap_index_recursive",
    "MAX_SITEMAP_URLS_PER_ORG",
    "MAX_SITEMAPS_PER_ORG",
    "MAX_SITEMAP_DEPTH",
]
