"""Per-org discovery pipeline (AC5, plus AC1/2/3/4/8.1 integration).

Order of operations per the spec:
  1. Fetch robots.txt; short-circuit with `fetch_status='forbidden'`
     if the whole site is disallowed for our UA.
  2. Fetch sitemap(s) if linked from robots or at /sitemap.xml;
     parse with `sitemap.parse_sitemap_index_recursive`.
  3. Fetch homepage; extract candidates via `candidate_filter`.
  4. For each candidate HTML page (not a PDF), fetch ONE subpage level
     and re-apply the filters. Cap: 5 subpages.
  5. Deduplicate candidates on canonical URL.
  6. Cap aggregate at CANDIDATE_CAP_PER_ORG.

The module takes a fetcher callback `(url, kind) -> (bytes, status)` so
tests can stub network I/O and production wires this to the Phase-1
HTTP client.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

from . import config
from . import sitemap as _sitemap
from .candidate_filter import (
    CANDIDATE_CAP_PER_ORG,
    Candidate,
    _anchor_matches,
    _path_matches,
    classify_sitemap_url,
    extract_candidates,
)
from .redirect_policy import etld1
from .robots import can_fetch as robots_can_fetch, sitemap_urls_from_robots
from .url_redact import canonicalize_url

MAX_SUBPAGES_PER_ORG = config.MAX_SUBPAGES_PER_ORG

Fetcher = Callable[[str, str], tuple[bytes, str]]

_log = logging.getLogger(__name__)

_REPORT_PATH_KEYWORDS = frozenset({
    "/annual-report", "/annualreport", "/annual_report",
    "/impact", "/our-impact",
    "/financials", "/financial-statements",
    "/transparency", "/accountability",
    "/reports", "/year-in-review",
})
_HIGH_VALUE_PATH_KEYWORDS = frozenset({
    "/about", "/our-work", "/what-we-do",
    "/publications", "/resources",
    "/support-us", "/giving", "/donate",
})


def _subpage_priority(c: Candidate) -> tuple[int, str]:
    """Score a subpage candidate for walk priority (lower = better)."""
    path = urlsplit(c.url).path.lower()
    if any(kw in path for kw in _REPORT_PATH_KEYWORDS):
        return (0, path)
    if _anchor_matches(c.anchor_text):
        return (1, path)
    if any(kw in path for kw in _HIGH_VALUE_PATH_KEYWORDS):
        return (2, path)
    return (3, path)


def _is_html_subpage_candidate(c: Candidate) -> bool:
    """Only follow same-domain HTML candidates into subpages."""
    if c.hosting_platform is not None and c.hosting_platform != "own-domain":
        return False
    if c.url.lower().endswith(".pdf"):
        return False
    return True


def per_org_candidates(
    *,
    seed_url: str,
    seed_etld1: str,
    fetcher: Fetcher,
    robots_text: str,
    ein: str = "",
) -> list[Candidate]:
    """Return the capped, deduped list of candidates for this org.

    `fetcher(url, kind)` returns `(body, status)` where `status` is
    either 'ok' or a spec-enum fetch_status. Non-ok responses are
    logged and skipped.
    """
    candidates: list[Candidate] = []
    seen_canonical: set[str] = set()
    ua = config.USER_AGENT
    home_base = seed_url.rstrip("/") or seed_url

    def _remember(c: Candidate) -> bool:
        canonical = canonicalize_url(c.url)
        if canonical in seen_canonical:
            return False
        seen_canonical.add(canonical)
        candidates.append(c)
        return len(candidates) >= CANDIDATE_CAP_PER_ORG

    # --- robots gate -----
    def _allowed(path: str) -> bool:
        return robots_can_fetch(robots_text, path, user_agent=ua)

    # --- TICK-004: sitemap discovery phase (BEFORE homepage) -----
    # AC2: try each robots Sitemap: directive. AC3: fallback to
    # /sitemap.xml only when robots has zero directives.
    # AC4-5: individual failures don't halt the org's crawl.
    def _sitemap_fetcher(url: str) -> bytes | None:
        s_parsed = urlsplit(url)
        # AC3: robots disallow on the sitemap path itself → skip.
        if etld1(s_parsed.hostname or "") == seed_etld1:
            if not _allowed(s_parsed.path or "/"):
                return None
        body, status = fetcher(url, "sitemap")
        if status != "ok" or not body:
            return None
        return body

    robots_sitemap_urls = sitemap_urls_from_robots(robots_text)
    if robots_sitemap_urls:
        sitemap_index_urls = robots_sitemap_urls
    else:
        sitemap_index_urls = [home_base.rstrip("/") + "/sitemap.xml"]

    # Import locally to avoid circular import at module load.
    from . import sitemap as _sitemap_mod

    def _parse_sitemap_any(url: str) -> list[str]:
        """Handle both sitemap-index and top-level urlset formats.
        Many small sites publish a single sitemap.xml that IS a urlset.
        """
        body = _sitemap_fetcher(url)
        if not body:
            return []
        try:
            # Try as index first; if no children, try as urlset.
            idx_urls = _sitemap_mod.parse_sitemap_index_recursive(
                url, fetcher=_sitemap_fetcher
            )
            if idx_urls:
                return idx_urls
            # Fallback: parse as top-level urlset.
            return _sitemap_mod.parse_sitemap(body)
        except Exception as exc:  # noqa: BLE001
            _log.info("discover: sitemap parse failed for %s: %s", url, exc)
            return []

    sitemap_urls: list[str] = []
    for idx_url in sitemap_index_urls:
        sitemap_urls.extend(_parse_sitemap_any(idx_url))

    # TICK-007: sitemap URLs that are HTML report-anchor pages need
    # the same subpage-expansion treatment that homepage-derived
    # candidates get — otherwise a site whose sitemap lists
    # /annual-report/ (HTML landing page) but NOT the /wp-content/
    # uploads/*.pdf inside it will never surface the PDF. Collected
    # here and fed into the shared subpages_to_walk below.
    sitemap_subpages_to_walk: list[Candidate] = []

    for s_url in sitemap_urls:
        canonical = canonicalize_url(s_url)
        parsed = urlsplit(canonical)
        # Robots gate AFTER canonicalization (AC7-8).
        if etld1(parsed.hostname or "") == seed_etld1:
            if not _allowed(parsed.path or "/"):
                continue
        c = classify_sitemap_url(
            url=canonical,
            seed_etld1=seed_etld1,
            referring_page_url=home_base,
        )
        if c is None:
            continue
        _remember(c)
        if len(candidates) >= CANDIDATE_CAP_PER_ORG:
            break
        # TICK-007: if this sitemap URL is a same-domain HTML page
        # whose path matched a report keyword, queue it for subpage
        # expansion so TICK-001's relaxed PDF filter can fire on its
        # children. PDFs in the sitemap itself don't need expansion.
        if not canonical.lower().endswith(".pdf"):
            if c.hosting_platform == "own-domain":
                sitemap_subpages_to_walk.append(c)

    # --- homepage -----
    if not _allowed("/"):
        _log.info("discover: robots disallows / for %s", home_base)
        return candidates  # AC4: still return any sitemap-derived candidates.

    homepage_subpages: list[Candidate] = []

    home_body, home_status = fetcher(home_base, "homepage")
    if home_status == "ok" and home_body:
        page_candidates = extract_candidates(
            html=home_body.decode("utf-8", errors="replace"),
            base_url=home_base + "/",
            seed_etld1=seed_etld1,
            referring_page_url=home_base,
            discovered_via="homepage-link",
            ein=ein,
        )
        for c in page_candidates:
            # robots gate on on-domain URLs.
            parsed = urlsplit(c.url)
            if etld1(parsed.hostname or "") == seed_etld1:
                if not _allowed(parsed.path or "/"):
                    continue
            _remember(c)
            if _is_html_subpage_candidate(c):
                homepage_subpages.append(c)

    # Homepage-derived subpages first (curated navigation links),
    # then sitemap-derived ones as fallback (TICK-007 pattern for
    # WAF-gated homepages). Dedup, then sort by report-relevance
    # so the 10-subpage cap hits the most valuable pages first.
    _subpage_seen: set[str] = set()
    subpages_to_walk: list[Candidate] = []
    for c in homepage_subpages + sitemap_subpages_to_walk:
        canon = canonicalize_url(c.url)
        if canon not in _subpage_seen:
            _subpage_seen.add(canon)
            subpages_to_walk.append(c)
    subpages_to_walk.sort(key=_subpage_priority)

    # --- one-hop subpages (runs regardless of homepage outcome) -----
    _log.info("discover: ein=%s subpages_queued=%d (cap=%d)",
              ein, len(subpages_to_walk), MAX_SUBPAGES_PER_ORG)
    if subpages_to_walk:
        for i, sub in enumerate(subpages_to_walk[:MAX_SUBPAGES_PER_ORG]):
            sub_parsed = urlsplit(sub.url)
            if etld1(sub_parsed.hostname or "") != seed_etld1:
                _log.info("discover: ein=%s subpage[%d] skip cross-origin: %s", ein, i, sub.url)
                continue
            if not _allowed(sub_parsed.path or "/"):
                _log.info("discover: ein=%s subpage[%d] skip robots: %s", ein, i, sub.url)
                continue
            sub_body, sub_status = fetcher(sub.url, "subpage")
            if sub_status != "ok" or not sub_body:
                _log.info("discover: ein=%s subpage[%d] fetch failed (%s): %s",
                          ein, i, sub_status, sub.url)
                continue
            _log.info("discover: ein=%s subpage[%d] fetched %d bytes: %s",
                      ein, i, len(sub_body), sub.url)
            # TICK-001: compute parent_is_report_anchor from the
            # subpage's OWN URL/anchor metadata. If the subpage was
            # chosen for expansion because its own path or its
            # referring-anchor matched a report keyword, relax the
            # strict PDF filter for links found inside it.
            parent_is_report_anchor = (
                _anchor_matches(sub.anchor_text)
                or _path_matches(sub_parsed.path or "")
            )
            sub_candidates = extract_candidates(
                html=sub_body.decode("utf-8", errors="replace"),
                base_url=sub.url,
                seed_etld1=seed_etld1,
                referring_page_url=sub.url,
                discovered_via="subpage-link",
                parent_is_report_anchor=parent_is_report_anchor,
                ein=ein,
            )
            for c in sub_candidates:
                parsed = urlsplit(c.url)
                if etld1(parsed.hostname or "") == seed_etld1 and not _allowed(
                    parsed.path or "/"
                ):
                    continue
                _remember(c)

    # Prioritize PDF candidates over HTML pages since the crawler
    # downloads PDFs — HTML candidates that survive to the download
    # phase just waste a request.
    candidates.sort(key=lambda c: (0 if c.url.lower().endswith(".pdf") else 1))
    return candidates[:CANDIDATE_CAP_PER_ORG]


__all__ = ["per_org_candidates", "MAX_SUBPAGES_PER_ORG"]
