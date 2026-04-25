"""Async per-org discovery pipeline (Spec 0021, AC26/AC28/AC29).

Reimplements discover.per_org_candidates using async I/O while reusing
all pure functions from candidate_filter, robots, sitemap, etc.
The synchronous discover.py is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import config
from . import sitemap as _sitemap
from .sitemap import _locs
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
from .wayback_fallback import WaybackOutcome, discover_via_wayback

if TYPE_CHECKING:
    from .async_crawler import CrawlStats
    from .async_http_client import AsyncHTTPClient

MAX_SUBPAGES_PER_ORG = config.MAX_SUBPAGES_PER_ORG

_log = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    homepage_ok: bool = False
    robots_disallowed_all: bool = False
    homepage_failure_reason: str | None = None
    wayback_outcome: WaybackOutcome | None = None
    wayback_capture_hosts: list[str] = field(default_factory=list)
    wayback_raw_row_count: int = 0
    wayback_validated_row_count: int = 0
    wayback_elapsed_ms: int = 0
    wayback_cdx_http_status: int | None = None

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
    path = urlsplit(c.url).path.lower()
    if any(kw in path for kw in _REPORT_PATH_KEYWORDS):
        return (0, path)
    if _anchor_matches(c.anchor_text):
        return (1, path)
    if any(kw in path for kw in _HIGH_VALUE_PATH_KEYWORDS):
        return (2, path)
    return (3, path)


def _is_html_subpage_candidate(c: Candidate) -> bool:
    if c.hosting_platform is not None and c.hosting_platform != "own-domain":
        return False
    if c.url.lower().endswith(".pdf"):
        return False
    return True


AsyncFetcher = Callable[[str, str], Awaitable[tuple[bytes, str]]]


async def discover_org(
    *,
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    robots_text: str,
    ein: str = "",
    fetcher: AsyncFetcher | None = None,
    stats: CrawlStats | None = None,
) -> DiscoveryResult:
    """Async equivalent of discover.per_org_candidates.

    When ``fetcher`` is supplied it is called as ``await fetcher(url, kind)``
    for homepage/subpage/sitemap GETs — this is how the caller injects retry
    logic and fetch-log recording (AC8 parity with the sync crawler).
    """
    result = DiscoveryResult()
    candidates: list[Candidate] = []
    seen_canonical: set[str] = set()
    ua = config.USER_AGENT
    home_base = seed_url.rstrip("/") or seed_url

    async def _default_fetch(url: str, kind: str) -> tuple[bytes, str]:
        r = await client.get(url, kind=kind, seed_etld1=seed_etld1)
        return (r.body or b""), r.status

    _fetch = fetcher or _default_fetch

    def _remember(c: Candidate) -> bool:
        canonical = canonicalize_url(c.url)
        if canonical in seen_canonical:
            return False
        seen_canonical.add(canonical)
        candidates.append(c)
        return len(candidates) >= CANDIDATE_CAP_PER_ORG

    def _allowed(path: str) -> bool:
        return robots_can_fetch(robots_text, path, user_agent=ua)

    async def _sitemap_fetcher_async(url: str) -> bytes | None:
        s_parsed = urlsplit(url)
        if etld1(s_parsed.hostname or "") == seed_etld1:
            if not _allowed(s_parsed.path or "/"):
                return None
        body, status = await _fetch(url, "sitemap")
        if status != "ok" or not body:
            return None
        return body

    robots_sitemap_urls = sitemap_urls_from_robots(robots_text)
    if robots_sitemap_urls:
        sitemap_index_urls = robots_sitemap_urls
    else:
        sitemap_index_urls = [home_base.rstrip("/") + "/sitemap.xml"]

    sitemap_urls: list[str] = []
    for idx_url in sitemap_index_urls:
        body = await _sitemap_fetcher_async(idx_url)
        if not body:
            continue
        try:
            direct_urls = _sitemap.parse_sitemap(body)
            if direct_urls:
                sitemap_urls.extend(direct_urls)
            else:
                child_locs = _locs(body, want="sitemap")[:config.MAX_SITEMAPS_PER_ORG]
                for child_url in child_locs:
                    child_body = await _sitemap_fetcher_async(child_url)
                    if not child_body:
                        continue
                    child_parsed = _sitemap.parse_sitemap(child_body)
                    sitemap_urls.extend(child_parsed)
                    if len(sitemap_urls) >= config.MAX_SITEMAP_URLS_PER_ORG:
                        break
        except Exception as exc:  # noqa: BLE001
            _log.info("async_discover: sitemap parse failed for %s: %s", idx_url, exc)

    sitemap_subpages_to_walk: list[Candidate] = []

    for s_url in sitemap_urls:
        canonical = canonicalize_url(s_url)
        parsed = urlsplit(canonical)
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
        if not canonical.lower().endswith(".pdf"):
            if c.hosting_platform == "own-domain":
                sitemap_subpages_to_walk.append(c)

    # --- homepage ---
    if not _allowed("/"):
        _log.info("async_discover: robots disallows / for %s", home_base)
        result.robots_disallowed_all = True
        result.candidates = candidates
        return result

    homepage_subpages: list[Candidate] = []

    home_body, home_status = await _fetch(home_base, "homepage")
    result.homepage_ok = home_status == "ok"
    if not result.homepage_ok:
        result.homepage_failure_reason = _classify_homepage_failure(home_status)
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
            parsed = urlsplit(c.url)
            if etld1(parsed.hostname or "") == seed_etld1:
                if not _allowed(parsed.path or "/"):
                    continue
            _remember(c)
            if _is_html_subpage_candidate(c):
                homepage_subpages.append(c)

    _subpage_seen: set[str] = set()
    subpages_to_walk: list[Candidate] = []
    for c in homepage_subpages + sitemap_subpages_to_walk:
        canon = canonicalize_url(c.url)
        if canon not in _subpage_seen:
            _subpage_seen.add(canon)
            subpages_to_walk.append(c)
    subpages_to_walk.sort(key=_subpage_priority)

    # --- one-hop subpages ---
    _log.info("async_discover: ein=%s subpages_queued=%d (cap=%d)",
              ein, len(subpages_to_walk), MAX_SUBPAGES_PER_ORG)
    if subpages_to_walk:
        for i, sub in enumerate(subpages_to_walk[:MAX_SUBPAGES_PER_ORG]):
            sub_parsed = urlsplit(sub.url)
            if etld1(sub_parsed.hostname or "") != seed_etld1:
                continue
            if not _allowed(sub_parsed.path or "/"):
                continue
            sub_body, sub_status = await _fetch(sub.url, "subpage")
            if sub_status != "ok" or not sub_body:
                continue
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

    candidates.sort(key=lambda c: (0 if c.url.lower().endswith(".pdf") else 1))
    result.candidates = candidates[:CANDIDATE_CAP_PER_ORG]

    # AC1: Wayback fallback gate
    if (
        not result.candidates
        and not result.homepage_ok
        and not result.robots_disallowed_all
        and stats is not None
    ):
        wayback = await discover_via_wayback(
            seed_url=seed_url,
            seed_etld1=seed_etld1,
            client=client,
            ein=ein,
            stats=stats,
        )
        result.wayback_outcome = wayback.outcome
        result.wayback_capture_hosts = wayback.capture_hosts
        result.wayback_raw_row_count = wayback.raw_row_count
        result.wayback_validated_row_count = wayback.validated_row_count
        result.wayback_elapsed_ms = wayback.elapsed_ms
        result.wayback_cdx_http_status = wayback.cdx_http_status
        if wayback.outcome == WaybackOutcome.RECOVERED:
            result.candidates = wayback.candidates

    return result


def _classify_homepage_failure(status: str) -> str:
    """AC3.1: bounded enum of failure reasons."""
    if status == "forbidden":
        return "homepage_cloudflare_challenge"
    if status == "not_found":
        return "homepage_4xx"
    if status == "server_error":
        return "homepage_5xx"
    if status == "network_error":
        return "homepage_network_error"
    if status == "size_capped":
        return "homepage_size_capped"
    if status == "blocked_content_type":
        return "homepage_blocked_content_type"
    return "homepage_unknown"


__all__ = ["discover_org", "DiscoveryResult", "MAX_SUBPAGES_PER_ORG"]
