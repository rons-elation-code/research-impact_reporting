"""Brave Web Search client with domain blocklist and rate limiting (Spec 0018).

Standalone client that queries the Brave Search API, filters results through
a domain blocklist using suffix matching, and enforces a global QPS rate limit.
API keys are never logged at any level.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

BLOCKLIST_DOMAINS: frozenset[str] = frozenset({
    "guidestar.org",
    "propublica.org",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "yelp.com",
    "candid.org",
    "causeiq.com",
    "charitynavigator.org",
    "idealist.org",
    "give.org",
    "benevity.org",
    "mapquest.com",
    "chamberofcommerce.com",
    "rocketreach.co",
    "wikipedia.org",
    "dnb.com",
    "instagram.com",
    "youtube.com",
    "taxexemptworld.com",
    "givefreely.com",
    "greatnonprofits.org",
    "nonprofitfacts.com",
})

BLOCKLIST_GOV_EXEMPT_WORDS = {"authority", "commission"}

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAYS = [2.0, 4.0, 8.0]


class BraveSearchError(RuntimeError):
    """Raised when the Brave API fails after all retries."""


@dataclass(frozen=True)
class BraveSearchResult:
    title: str
    url: str
    snippet: str


class BraveRateLimiter:
    """Token-bucket rate limiter, thread-safe.

    Releases one permit per 1/qps seconds. Retries do NOT consume a new
    permit — the caller acquires once before the first attempt and reuses
    the permit across retries (AC25).
    """

    def __init__(self, qps: float) -> None:
        if qps <= 0:
            raise ValueError("qps must be positive")
        self._interval = 1.0 / qps
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                wait = self._next_allowed - now
                self._next_allowed += self._interval
            else:
                wait = 0.0
                self._next_allowed = now + self._interval
        if wait > 0:
            time.sleep(wait)


def is_blocked(domain: str, org_name: str) -> bool:
    """Suffix-match against BLOCKLIST_DOMAINS.

    *.gov is blocked unless org_name contains 'authority' or 'commission'
    (case-insensitive). Suffix matching: linkedin.com matches
    www.linkedin.com and au.linkedin.com, but NOT linkedin-example.com.
    """
    domain = domain.lower()

    if domain.endswith(".gov"):
        name_lower = (org_name or "").lower()
        if any(w in name_lower for w in BLOCKLIST_GOV_EXEMPT_WORDS):
            return False
        return True

    for blocked in BLOCKLIST_DOMAINS:
        if domain == blocked or domain.endswith("." + blocked):
            return True

    return False


def search(
    query: str,
    *,
    api_key: str,
    count: int = 10,
    rate_limiter: BraveRateLimiter,
) -> list[BraveSearchResult]:
    """Search the Brave Web Search API.

    Retries up to 3 times on 429/5xx with exponential backoff. Retries
    reuse the rate limiter permit (AC25) — acquire happens once before
    the first attempt. Raises BraveSearchError on exhaustion.
    """
    rate_limiter.acquire()

    last_exc: Exception | None = None
    for attempt, delay in enumerate(
        [0.0] + _RETRY_DELAYS, start=1
    ):
        if attempt > 1:
            time.sleep(delay)

        try:
            resp = requests.get(
                _BRAVE_URL,
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                },
                params={"q": query, "count": count, "safesearch": "moderate"},
                timeout=30,
            )
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("Brave search network error attempt=%d", attempt)
            if attempt > len(_RETRY_DELAYS):
                break
            continue

        if resp.status_code == 200:
            data = resp.json()
            results = (data.get("web") or {}).get("results") or []
            return [
                BraveSearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("description", ""),
                )
                for r in results
            ]

        if resp.status_code not in _RETRY_STATUSES:
            raise BraveSearchError(
                f"Brave API returned {resp.status_code}"
            )

        log.warning(
            "Brave search error status=%d attempt=%d",
            resp.status_code,
            attempt,
        )
        last_exc = BraveSearchError(
            f"Brave API returned {resp.status_code}"
        )
        if attempt > len(_RETRY_DELAYS):
            break

    raise BraveSearchError(
        f"Brave API failed after retries: {last_exc}"
    )


def search_and_filter(
    org_name: str,
    city: str,
    state: str,
    *,
    api_key: str,
    rate_limiter: BraveRateLimiter,
    max_results: int = 3,
) -> list[BraveSearchResult]:
    """Build query, search, filter blocklist, return top results.

    Sanitizes org_name to prevent query manipulation via embedded quotes.
    """
    sanitized_name = re.sub(r'"', "", org_name or "").strip()
    query = f'"{sanitized_name}" {city} {state}'

    results = search(
        query,
        api_key=api_key,
        rate_limiter=rate_limiter,
    )

    filtered: list[BraveSearchResult] = []
    for r in results:
        try:
            host = urlsplit(r.url).hostname or ""
        except Exception:
            continue
        if is_blocked(host, org_name):
            continue
        filtered.append(r)
        if len(filtered) >= max_results:
            break

    return filtered


__all__ = [
    "BLOCKLIST_DOMAINS",
    "BLOCKLIST_GOV_EXEMPT_WORDS",
    "BraveRateLimiter",
    "BraveSearchError",
    "BraveSearchResult",
    "is_blocked",
    "search",
    "search_and_filter",
]
