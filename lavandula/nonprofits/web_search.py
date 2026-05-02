"""Unified search adapter with Serpex multi-engine support (Spec 0031).

Drop-in replacement for brave_search as the search interface for the
resolver pipeline. Supports both Serpex (default) and Brave direct backends.
Serpex enables multi-engine queries (brave, google, bing) with result merging.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import NamedTuple
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

VALID_ENGINES = frozenset({"brave", "google", "bing", "auto"})

_SERPEX_URL = "https://api.serpex.dev/api/search"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAYS = [2.0, 4.0, 8.0]

_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(?:Inc|Corp|Corporation|LLC|Ltd|Co|Foundation|Trust|Assn|Association|Pc)\s*$",
    re.I,
)


class SearchError(RuntimeError):
    """Raised when search fails after all retries (or all engines fail)."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    engines: tuple[str, ...]


@dataclass
class SearchConfig:
    backend: str          # "serpex" | "brave-direct"
    engines: list[str]    # ["brave"] or ["brave", "google"]
    api_key: str
    qps: float
    count: int = 10

    def __repr__(self) -> str:
        return (
            f"SearchConfig(backend={self.backend!r}, engines={self.engines!r}, "
            f"api_key='***', qps={self.qps}, count={self.count})"
        )


class RateLimiter:
    """Token-bucket rate limiter, thread-safe."""

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


# ── Search Stats ─────────────────────────────────────────────────────────────


@dataclass
class SearchStats:
    successful_by_engine: dict[str, int] = field(default_factory=dict)
    failed_by_engine: dict[str, int] = field(default_factory=dict)
    search_full: int = 0
    search_partial: int = 0
    search_failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def estimated_credits(self) -> int:
        return sum(self.successful_by_engine.values())

    def record_success(self, engine: str) -> None:
        with self._lock:
            self.successful_by_engine[engine] = self.successful_by_engine.get(engine, 0) + 1

    def record_failure(self, engine: str) -> None:
        with self._lock:
            self.failed_by_engine[engine] = self.failed_by_engine.get(engine, 0) + 1

    def record_query_outcome(self, *, total_engines: int, failed_engines: int) -> None:
        with self._lock:
            if failed_engines == 0:
                self.search_full += 1
            elif failed_engines < total_engines:
                self.search_partial += 1
            else:
                self.search_failed += 1


_search_stats = SearchStats()


def get_search_stats() -> SearchStats:
    return _search_stats


def reset_search_stats() -> None:
    global _search_stats
    _search_stats = SearchStats()


# ── URL normalization ────────────────────────────────────────────────────────


def _normalize_url(url: str) -> tuple[str, str]:
    """Normalize URL for dedup. Returns (normalized_key, preferred_url).

    The normalized key drops scheme and fragments, collapses www, trailing /.
    The preferred_url is the original URL (https preferred over http).
    """
    try:
        parts = urlsplit(url)
    except Exception:
        return (url, url)

    hostname = (parts.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    port = parts.port
    if port in (80, 443, None):
        host = hostname
    else:
        host = f"{hostname}:{port}"

    path = parts.path.rstrip("/") if parts.path != "/" else ""

    query = parts.query

    if query:
        key = f"{host}{path}?{query}"
    else:
        key = f"{host}{path}"

    return (key, url)


# ── Engine validation ────────────────────────────────────────────────────────


def validate_engines(engines: list[str]) -> list[str]:
    """Validate and deduplicate engine list. Raises ValueError on invalid input."""
    seen = set()
    cleaned = []
    for e in engines:
        e = e.strip().lower()
        if not e:
            continue
        if e not in VALID_ENGINES:
            raise ValueError(
                f"Unknown search engine {e!r}. Valid engines: {sorted(VALID_ENGINES)}"
            )
        if e not in seen:
            seen.add(e)
            cleaned.append(e)

    if not cleaned:
        raise ValueError("At least one search engine must be specified")

    if "auto" in cleaned and len(cleaned) > 1:
        raise ValueError("'auto' cannot be combined with other engines")

    return cleaned


# ── Serpex backend ───────────────────────────────────────────────────────────


def _serpex_search(
    query: str,
    engine: str,
    *,
    api_key: str,
    count: int,
    rate_limiter: RateLimiter,
) -> list[SearchResult]:
    """Single Serpex API call for one engine."""
    rate_limiter.acquire()
    t0 = time.monotonic()

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0.0] + _RETRY_DELAYS, start=1):
        if attempt > 1:
            time.sleep(delay)

        try:
            resp = requests.get(
                _SERPEX_URL,
                headers={"X-API-Key": api_key},
                params={"q": query, "engine": engine, "category": "web"},
                timeout=30,
            )
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("Serpex network error engine=%s attempt=%d", engine, attempt)
            if attempt > len(_RETRY_DELAYS):
                break
            continue

        if resp.status_code == 200:
            elapsed_ms = (time.monotonic() - t0) * 1000
            data = resp.json()
            raw_results = data.get("results") or []
            results = [
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("snippet", ""),
                    engines=(engine,),
                )
                for r in raw_results[:count]
            ]
            log.debug(
                "search.engine=%s query=%s results=%d elapsed=%.0fms",
                engine, query[:50], len(results), elapsed_ms,
            )
            return results

        if resp.status_code == 402:
            raise SearchError(
                f"Serpex credit exhaustion (402) for engine={engine}"
            )

        if resp.status_code not in _RETRY_STATUSES:
            raise SearchError(
                f"Serpex API returned {resp.status_code} for engine={engine}"
            )

        log.warning(
            "Serpex error status=%d engine=%s attempt=%d",
            resp.status_code, engine, attempt,
        )
        last_exc = SearchError(
            f"Serpex API returned {resp.status_code}"
        )
        if attempt > len(_RETRY_DELAYS):
            break

    raise SearchError(
        f"Serpex API failed after retries for engine={engine}: {last_exc}"
    )


# ── Multi-engine merge ───────────────────────────────────────────────────────


@dataclass
class _MergeEntry:
    engines: set
    best_rank: int
    url: str
    result: SearchResult
    insertion_order: int


def _merge_results(
    results_by_engine: dict[str, list[SearchResult]],
) -> list[SearchResult]:
    """Merge results from multiple engines, dedup by normalized URL."""
    url_map: dict[str, _MergeEntry] = {}
    insertion_counter = 0

    for engine, results in results_by_engine.items():
        for rank, r in enumerate(results):
            norm_key, _ = _normalize_url(r.url)

            if norm_key in url_map:
                entry = url_map[norm_key]
                entry.engines.add(engine)
                entry.best_rank = min(entry.best_rank, rank)
                # https wins over http
                if r.url.startswith("https://") and not entry.url.startswith("https://"):
                    entry.url = r.url
                    entry.result = r
            else:
                url_map[norm_key] = _MergeEntry(
                    engines={engine},
                    best_rank=rank,
                    url=r.url,
                    result=r,
                    insertion_order=insertion_counter,
                )
                insertion_counter += 1

    sorted_entries = sorted(
        url_map.values(),
        key=lambda e: (-len(e.engines), e.best_rank, e.insertion_order),
    )

    merged = []
    for entry in sorted_entries:
        merged.append(SearchResult(
            title=entry.result.title,
            url=entry.url,
            snippet=entry.result.snippet,
            engines=tuple(sorted(entry.engines)),
        ))

    multi_hit = sum(1 for e in sorted_entries if len(e.engines) > 1)
    if len(results_by_engine) > 1:
        log.info(
            "search.multi engines=%s unique_urls=%d multi_hit_urls=%d",
            ",".join(results_by_engine.keys()),
            len(merged),
            multi_hit,
        )

    return merged


# ── Public API ───────────────────────────────────────────────────────────────


def search(
    query: str,
    *,
    config: SearchConfig,
    rate_limiter: RateLimiter,
) -> list[SearchResult]:
    """Query configured engine(s) and return results.

    Single engine: one Serpex API call.
    Multi engine: sequential calls, merge+dedupe.
    brave-direct backend: delegates to brave_search.search().
    """
    if config.backend == "brave-direct":
        from .brave_search import BraveRateLimiter, BraveSearchError, search as brave_search_fn
        brave_rl = BraveRateLimiter(config.qps)
        try:
            brave_results = brave_search_fn(
                query, api_key=config.api_key, count=config.count,
                rate_limiter=brave_rl,
            )
        except BraveSearchError as exc:
            raise SearchError(str(exc)) from exc
        return [
            SearchResult(
                title=r.title, url=r.url, snippet=r.snippet,
                engines=("brave",),
            )
            for r in brave_results
        ]

    engines = config.engines
    if len(engines) == 1:
        try:
            results = _serpex_search(
                query, engines[0],
                api_key=config.api_key, count=config.count,
                rate_limiter=rate_limiter,
            )
        except SearchError:
            _search_stats.record_failure(engines[0])
            _search_stats.record_query_outcome(total_engines=1, failed_engines=1)
            raise
        _search_stats.record_success(engines[0])
        _search_stats.record_query_outcome(total_engines=1, failed_engines=0)
        return results

    # Multi-engine
    results_by_engine: dict[str, list[SearchResult]] = {}
    failed_engines = 0
    for engine in engines:
        try:
            results_by_engine[engine] = _serpex_search(
                query, engine,
                api_key=config.api_key, count=config.count,
                rate_limiter=rate_limiter,
            )
            _search_stats.record_success(engine)
        except SearchError as exc:
            failed_engines += 1
            _search_stats.record_failure(engine)
            log.warning(
                "search.engine_failed engine=%s error=%s remaining_engines=%s",
                engine, exc, [e for e in engines if e != engine and e not in results_by_engine],
            )

    _search_stats.record_query_outcome(
        total_engines=len(engines), failed_engines=failed_engines,
    )

    if not results_by_engine:
        raise SearchError(
            f"All engines failed for query: {query[:50]}"
        )

    return _merge_results(results_by_engine)


# ── search_and_filter ────────────────────────────────────────────────────────


class SearchFilterResult(NamedTuple):
    results: list[SearchResult]
    had_raw_results: bool


def search_and_filter(
    org_name: str,
    city: str,
    state: str,
    *,
    config: SearchConfig,
    rate_limiter: RateLimiter,
    max_results: int = 3,
) -> SearchFilterResult:
    """Build query, search (single or multi), filter blocklist, return top N."""
    sanitized_name = re.sub(r'"', "", org_name or "").strip()
    clean_name = _LEGAL_SUFFIX_RE.sub("", sanitized_name).strip()
    query = f'{clean_name} {city} {state}'

    raw_results = search(query, config=config, rate_limiter=rate_limiter)

    if not raw_results:
        return SearchFilterResult([], False)

    from .brave_search import is_blocked

    filtered: list[SearchResult] = []
    for r in raw_results:
        try:
            host = urlsplit(r.url).hostname or ""
        except Exception:
            continue
        if is_blocked(host, org_name):
            continue
        filtered.append(r)
        if len(filtered) >= max_results:
            break

    return SearchFilterResult(filtered, True)


__all__ = [
    "RateLimiter",
    "SearchConfig",
    "SearchError",
    "SearchFilterResult",
    "SearchResult",
    "SearchStats",
    "VALID_ENGINES",
    "get_search_stats",
    "reset_search_stats",
    "search",
    "search_and_filter",
    "validate_engines",
]
