"""Wayback Machine CDX fallback for Cloudflare-blocked sites (Spec 0022).

When direct discovery yields zero candidates and the homepage is
unreachable (but robots didn't block), query the Wayback CDX API for
archived PDFs under the domain.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import config
from .candidate_filter import Candidate
from .logging_utils import sanitize
from .redirect_policy import etld1
from .wayback_validation import build_cdx_url, build_wayback_url, validate_cdx_row

if TYPE_CHECKING:
    from .async_crawler import CrawlStats
    from .async_http_client import AsyncHTTPClient

_log = logging.getLogger(__name__)


class WaybackOutcome(str, Enum):
    RECOVERED = "recovered"
    EMPTY = "empty"
    ERROR = "error"
    INVALID_DOMAIN = "invalid_domain"


@dataclass
class WaybackResult:
    outcome: WaybackOutcome
    candidates: list[Candidate] = field(default_factory=list)
    capture_hosts: list[str] = field(default_factory=list)
    raw_row_count: int = 0
    validated_row_count: int = 0
    elapsed_ms: int = 0
    cdx_http_status: int | None = None
    cdx_query_fired: bool = False


def _parse_cdx_response(body: bytes) -> tuple[WaybackOutcome, list[dict], int]:
    """Parse CDX JSON. Returns (outcome, validated_rows, raw_data_row_count)."""
    try:
        rows = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (WaybackOutcome.ERROR, [], 0)
    if not isinstance(rows, list) or len(rows) < 1:
        return (WaybackOutcome.EMPTY, [], 0)
    raw_data_rows = rows[1:]
    raw_count = len(raw_data_rows)
    validated = []
    for row in raw_data_rows:
        v = validate_cdx_row(row)
        if v is not None:
            validated.append(v)
    if not validated:
        return (WaybackOutcome.EMPTY, [], raw_count)
    return (WaybackOutcome.RECOVERED, validated, raw_count)


def _dedupe_and_cap(
    rows: list[dict],
    seed_etld1: str,
    max_pdfs: int,
    max_subdomains: int,
) -> tuple[list[dict], list[str]]:
    """Dedup by urlkey (max timestamp), filter by eTLD+1 ownership,
    cap subdomains (AC15.4), cap total count.
    """
    by_urlkey: dict[str, dict] = {}
    for r in rows:
        prev = by_urlkey.get(r["urlkey"])
        if prev is None or r["timestamp"] > prev["timestamp"]:
            by_urlkey[r["urlkey"]] = r
    candidates = sorted(
        by_urlkey.values(), key=lambda r: r["timestamp"], reverse=True,
    )

    # AC15.4: filter by eTLD+1 ownership
    filtered = [r for r in candidates if etld1(r["capture_host"]) == seed_etld1]

    # AC15.4: cap distinct subdomains, apex required if present
    apex = seed_etld1
    apex_candidates = [r for r in filtered if r["capture_host"] == apex]
    other_candidates = [r for r in filtered if r["capture_host"] != apex]
    subdomain_quota = max_subdomains - (1 if apex_candidates else 0)

    distinct_other_hosts: list[str] = []
    kept_other: list[dict] = []
    for r in other_candidates:
        host = r["capture_host"]
        if host in distinct_other_hosts:
            kept_other.append(r)
        elif len(distinct_other_hosts) < subdomain_quota:
            distinct_other_hosts.append(host)
            kept_other.append(r)

    final = (apex_candidates + kept_other)[:max_pdfs]
    capture_hosts = sorted({r["capture_host"] for r in final})
    return (final, capture_hosts)


def _row_to_candidate(row: dict, seed_url: str) -> Candidate:
    """Build a Candidate with Wayback attribution per AC11."""
    wayback_url = build_wayback_url(row["timestamp"], row["original"])
    return Candidate(
        url=wayback_url,
        referring_page_url=seed_url,
        anchor_text=row["original"],
        discovered_via="wayback",
        hosting_platform="wayback",
        attribution_confidence="wayback_archive",
        original_source_url=row["original"],
        wayback_digest=row.get("digest"),
    )


async def discover_via_wayback(
    *,
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    ein: str,
    stats: CrawlStats,
) -> WaybackResult:
    """Query Wayback CDX for PDFs under the domain."""
    if not config.WAYBACK_ENABLED:
        return WaybackResult(outcome=WaybackOutcome.ERROR, elapsed_ms=0, cdx_query_fired=False)

    loop = asyncio.get_running_loop()
    t_start = loop.time()
    domain = urlsplit(seed_url).hostname or seed_etld1
    cdx_url = build_cdx_url(domain)
    if cdx_url is None:
        return WaybackResult(
            outcome=WaybackOutcome.INVALID_DOMAIN,
            elapsed_ms=0,
            cdx_query_fired=False,
        )

    stats.wayback_attempts += 1

    r = await client.get(cdx_url, kind="wayback-cdx", timeout_override=15.0)
    elapsed = int((loop.time() - t_start) * 1000)

    if r.status != "ok" or not r.body:
        return WaybackResult(
            outcome=WaybackOutcome.ERROR,
            elapsed_ms=elapsed,
            cdx_http_status=r.http_status,
            cdx_query_fired=True,
        )

    outcome, validated, raw_count = _parse_cdx_response(r.body)
    if outcome != WaybackOutcome.RECOVERED:
        return WaybackResult(
            outcome=outcome,
            raw_row_count=raw_count,
            validated_row_count=0,
            elapsed_ms=elapsed,
            cdx_http_status=r.http_status,
            cdx_query_fired=True,
        )

    deduped, capture_hosts = _dedupe_and_cap(
        validated,
        seed_etld1=seed_etld1,
        max_pdfs=config.WAYBACK_MAX_PDFS_PER_ORG,
        max_subdomains=config.WAYBACK_MAX_DISTINCT_SUBDOMAINS,
    )
    if not deduped:
        return WaybackResult(
            outcome=WaybackOutcome.EMPTY,
            raw_row_count=raw_count,
            validated_row_count=len(validated),
            elapsed_ms=elapsed,
            cdx_http_status=r.http_status,
            cdx_query_fired=True,
        )

    candidates = [_row_to_candidate(row, seed_url) for row in deduped]
    return WaybackResult(
        outcome=WaybackOutcome.RECOVERED,
        candidates=candidates,
        capture_hosts=capture_hosts,
        raw_row_count=raw_count,
        validated_row_count=len(validated),
        elapsed_ms=elapsed,
        cdx_http_status=r.http_status,
        cdx_query_fired=True,
    )


__all__ = [
    "WaybackOutcome",
    "WaybackResult",
    "discover_via_wayback",
]
