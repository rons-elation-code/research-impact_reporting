"""Producer-consumer pipeline for nonprofit URL resolution (Spec 0018).

Architecture: Code handles search (Brave API), filtering (domain blocklist),
and HTTP fetching. The LLM (Gemma 4 E4B) is called exactly once per org to
disambiguate pre-fetched candidates. No agent loops, no tool-calling by the
LLM to drive search.

Threading model: one producer thread runs Stages 1-4 (search + filter + fetch),
filling a bounded queue. The consumer runs in the main thread (Stages 5-6),
pulling packets and calling Gemma sequentially.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests as http_requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .brave_search import (
    BraveRateLimiter,
    BraveSearchError,
    search,
    search_and_filter,
)
from .gemma_client import (
    RESOLVER_METHOD,
    GemmaClient,
    GemmaParseError,
)
from .url_normalize import normalize_url

log = logging.getLogger(__name__)

_SCHEMA = "lava_impact"
_SENTINEL = None
_RETRY_DELAYS = [5, 10, 20]


# ── Pipeline Queue ────────────────────────────────────────────────────────────


class PipelineQueue:
    """Bounded producer-consumer queue with sentinel-based shutdown."""

    def __init__(self, maxsize: int = 32) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)

    def put(self, packet: dict, timeout: float = 60.0) -> None:
        self._q.put(packet, timeout=timeout)

    def get(self, timeout: float = 60.0) -> dict | None:
        return self._q.get(timeout=timeout)

    def done(self) -> None:
        self._q.put(_SENTINEL)

    @property
    def qsize(self) -> int:
        return self._q.qsize()


class ShutdownFlag:
    """Cooperative shutdown. SIGINT sets this; producer checks before each org."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


def install_sigint_handler(flag: ShutdownFlag) -> None:
    """Install a SIGINT handler that sets the shutdown flag.

    Second Ctrl-C restores default behavior (hard kill).
    """
    def handler(signum, frame):
        log.info("SIGINT received — shutting down gracefully")
        flag.set()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, handler)


# ── Stats ─────────────────────────────────────────────────────────────────────


@dataclass
class ProducerStats:
    searched: int = 0
    enqueued: int = 0
    skipped_no_results: int = 0
    skipped_all_blocked: int = 0
    skipped_no_live: int = 0
    brave_errors: int = 0


@dataclass
class ConsumerStats:
    resolved: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    errors: int = 0
    max_queue_depth: int = 0


# ── Fetch helpers ─────────────────────────────────────────────────────────────


_tls = threading.local()


def _get_http_client():
    """Per-thread ReportsHTTPClient (AC21)."""
    if not hasattr(_tls, "client"):
        from lavandula.reports.http_client import ReportsHTTPClient
        _tls.client = ReportsHTTPClient(allow_insecure_cleartext=True)
    return _tls.client


def _extract_text(html_bytes: bytes, max_chars: int = 3000) -> str:
    """Extract visible text from HTML, stripping tags/scripts/styles."""
    text_str = html_bytes.decode("utf-8", errors="replace")
    text_str = re.sub(r"<script[^>]*>.*?</script>", " ", text_str, flags=re.DOTALL | re.IGNORECASE)
    text_str = re.sub(r"<style[^>]*>.*?</style>", " ", text_str, flags=re.DOTALL | re.IGNORECASE)
    text_str = re.sub(r"<[^>]+>", " ", text_str)
    text_str = re.sub(r"\s+", " ", text_str).strip()
    return text_str[:max_chars]


def _fetch_candidate(url: str) -> dict:
    """Fetch a single candidate URL, return a candidate dict."""
    client = _get_http_client()
    try:
        result = client.get(url, kind="resolver-verify")
    except Exception as exc:
        log.debug("Fetch error for %s: %s", url, type(exc).__name__)
        return {
            "url": url,
            "final_url": url,
            "live": False,
            "title": "",
            "snippet": "",
            "excerpt": "",
            "status_code": None,
        }

    final_url = result.final_url or url
    live = result.status == "ok" and result.body is not None
    excerpt = _extract_text(result.body) if live and result.body else ""

    return {
        "url": url,
        "final_url": final_url,
        "live": live,
        "title": "",
        "snippet": "",
        "excerpt": excerpt,
        "status_code": result.http_status,
    }


# ── DB helpers ────────────────────────────────────────────────────────────────


def _write_unresolved(
    engine: Engine,
    ein: str,
    reason: str,
    candidates_json: str | None = None,
) -> None:
    """Write an unresolved result directly to the DB."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {_SCHEMA}.nonprofits_seed SET "
                    "  website_url=NULL, resolver_status='unresolved', "
                    "  resolver_confidence=0, resolver_method=:method, "
                    "  resolver_reason=:reason, "
                    "  website_candidates_json=:cand "
                    "WHERE ein=:ein"
                ),
                {
                    "method": RESOLVER_METHOD,
                    "reason": reason,
                    "cand": candidates_json,
                    "ein": ein,
                },
            )
    except Exception:
        log.exception("DB write error for ein=%s reason=%s", ein, reason)


def _write_result(
    engine: Engine,
    ein: str,
    *,
    url: str | None,
    status: str,
    confidence: float,
    reason: str,
    candidates_json: str | None = None,
) -> None:
    """Write a resolver result to the DB (AC8)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {_SCHEMA}.nonprofits_seed SET "
                "  website_url=:url, resolver_status=:status, "
                "  resolver_confidence=:conf, resolver_method=:method, "
                "  resolver_reason=:reason, "
                "  website_candidates_json=:cand "
                "WHERE ein=:ein"
            ),
            {
                "url": url,
                "status": status,
                "conf": confidence,
                "method": RESOLVER_METHOD,
                "reason": reason,
                "cand": candidates_json,
                "ein": ein,
            },
        )


# ── Producer (Stages 1-4) ────────────────────────────────────────────────────


def producer(
    orgs: list[dict],
    *,
    pq: PipelineQueue,
    engine: Engine,
    api_key: str,
    rate_limiter: BraveRateLimiter,
    search_parallelism: int = 4,
    fetch_parallelism: int = 8,
    shutdown: ShutdownFlag,
) -> ProducerStats:
    """Run Stages 1-4 for each org, filling the queue with candidate packets."""
    stats = ProducerStats()

    try:
        for org in orgs:
            if shutdown.is_set():
                break

            ein = org["ein"]
            name = org.get("name", "")
            city = org.get("city", "")
            state = org.get("state", "")

            stats.searched += 1

            # Stage 1: Search
            try:
                raw_results = search(
                    f'"{re.sub(r"\"", "", name).strip()}" {city} {state}',
                    api_key=api_key,
                    rate_limiter=rate_limiter,
                )
            except BraveSearchError as exc:
                stats.brave_errors += 1
                reason_match = re.search(r"(\d{3})", str(exc))
                status_code = reason_match.group(1) if reason_match else "unknown"
                _write_unresolved(engine, ein, f"brave_error:{status_code}")
                continue

            if not raw_results:
                stats.skipped_no_results += 1
                _write_unresolved(engine, ein, "no_search_results")
                continue

            # Stage 2: Filter blocklist
            from .brave_search import is_blocked
            from urllib.parse import urlsplit as _urlsplit
            results = []
            for r in raw_results:
                try:
                    host = _urlsplit(r.url).hostname or ""
                except Exception:
                    continue
                if not is_blocked(host, name):
                    results.append(r)
                    if len(results) >= 3:
                        break

            if not results:
                stats.skipped_all_blocked += 1
                _write_unresolved(engine, ein, "all_blocked")
                continue

            # Stage 3: Fetch candidates in parallel
            candidates = []
            with ThreadPoolExecutor(max_workers=fetch_parallelism) as pool:
                futures = {
                    pool.submit(_fetch_candidate, r.url): r
                    for r in results
                }
                for future in as_completed(futures):
                    brave_result = futures[future]
                    cand = future.result()
                    cand["title"] = brave_result.title
                    cand["snippet"] = brave_result.snippet
                    candidates.append(cand)

            live = [c for c in candidates if c["live"]]
            if not live:
                stats.skipped_no_live += 1
                _write_unresolved(
                    engine, ein, "no_live_candidates",
                    json.dumps(candidates),
                )
                continue

            # Stage 4: Enqueue
            packet = {
                "ein": ein,
                "name": name,
                "city": city,
                "state": state,
                "address": org.get("address", ""),
                "zipcode": org.get("zipcode", ""),
                "ntee_code": org.get("ntee_code", ""),
                "candidates": live,
            }
            pq.put(packet)
            stats.enqueued += 1

    finally:
        pq.done()

    return stats


# ── Consumer (Stages 5-6) ────────────────────────────────────────────────────


def consumer(
    *,
    pq: PipelineQueue,
    gemma: GemmaClient,
    engine: Engine,
    shutdown: ShutdownFlag,
) -> ConsumerStats:
    """Pull candidate packets from the queue, disambiguate via Gemma, write results."""
    stats = ConsumerStats()

    while True:
        try:
            packet = pq.get(timeout=5.0)
        except queue.Empty:
            if shutdown.is_set():
                break
            continue

        if packet is _SENTINEL:
            break

        depth = pq.qsize
        if depth > stats.max_queue_depth:
            stats.max_queue_depth = depth

        ein = packet["ein"]
        org = {
            "ein": ein,
            "name": packet["name"],
            "city": packet["city"],
            "state": packet["state"],
            "address": packet.get("address", ""),
            "zipcode": packet.get("zipcode", ""),
            "ntee_code": packet.get("ntee_code", ""),
        }
        candidates = packet["candidates"]
        candidates_json = json.dumps(candidates)

        # Stage 5: Disambiguate via Gemma with retry
        result = None
        for attempt, delay in enumerate(
            [0] + _RETRY_DELAYS, start=1
        ):
            if attempt > 1:
                log.warning(
                    "Gemma retry attempt=%d for ein=%s (waiting %ds)",
                    attempt, ein, delay,
                )
                time.sleep(delay)
            try:
                result = gemma.disambiguate(org, candidates)
                break
            except http_requests.ConnectionError:
                if attempt > len(_RETRY_DELAYS):
                    log.error(
                        "Gemma unreachable after %d attempts for ein=%s",
                        attempt, ein,
                    )
                    result = None
            except GemmaParseError as exc:
                log.warning("Gemma parse error for ein=%s: %s", ein, exc)
                result = {"_parse_error": True}
                break

        # Handle failures
        if result is None:
            stats.unresolved += 1
            try:
                _write_result(
                    engine, ein,
                    url=None, status="unresolved",
                    confidence=0.0, reason="inference_unavailable",
                    candidates_json=candidates_json,
                )
            except Exception:
                stats.errors += 1
                log.exception("DB write error for ein=%s", ein)
            continue

        if result.get("_parse_error"):
            stats.unresolved += 1
            try:
                _write_result(
                    engine, ein,
                    url=None, status="unresolved",
                    confidence=0.0, reason="llm_parse_error",
                    candidates_json=candidates_json,
                )
            except Exception:
                stats.errors += 1
                log.exception("DB write error for ein=%s", ein)
            continue

        # Stage 6: Apply thresholds and write
        url = result.get("url")
        confidence = float(result.get("confidence", 0))
        reasoning = str(result.get("reasoning", ""))[:300]

        if url:
            try:
                url = normalize_url(url, check_https=True)
            except Exception:
                pass

        # Determine status — Gemma returns one URL+confidence per org.
        # Ambiguous: confidence 0.6-0.7 indicates the model is torn between
        # candidates (maps to the spec's "two candidates ≥ 0.6 within 0.1"
        # expressed as model uncertainty in the single-tool-call pattern).
        if confidence >= 0.7 and url:
            status = "resolved"
            stats.resolved += 1
        elif confidence >= 0.6 and url:
            status = "ambiguous"
            stats.ambiguous += 1
        else:
            status = "unresolved"
            url = None
            stats.unresolved += 1
            if not reasoning:
                reasoning = "no_confident_match"

        try:
            _write_result(
                engine, ein,
                url=url, status=status,
                confidence=confidence, reason=reasoning,
                candidates_json=candidates_json,
            )
        except Exception:
            stats.errors += 1
            log.exception("DB write error for ein=%s", ein)

    return stats


# ── Org loading ───────────────────────────────────────────────────────────────


def load_unresolved_orgs(
    engine: Engine,
    *,
    state: str,
    limit: int | None = None,
    status_filter: str = "unresolved",
) -> list[dict]:
    """Load orgs from nonprofits_seed that need resolution."""
    sql = (
        f"SELECT ein, name, address, city, state, zipcode, ntee_code "
        f"FROM {_SCHEMA}.nonprofits_seed "
        f"WHERE state=:state"
    )

    if status_filter == "unresolved":
        sql += " AND (resolver_status IS NULL OR resolver_status = 'unresolved')"
    else:
        sql += " AND resolver_status = :status_filter"

    sql += " ORDER BY ein"

    if limit:
        sql += " LIMIT :lim"

    params: dict = {"state": state}
    if status_filter != "unresolved":
        params["status_filter"] = status_filter
    if limit:
        params["lim"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    return [
        {
            "ein": row[0],
            "name": row[1] or "",
            "address": row[2] or "",
            "city": row[3] or "",
            "state": row[4] or "",
            "zipcode": row[5] or "",
            "ntee_code": row[6] or "",
        }
        for row in rows
    ]


# ── Dry run ───────────────────────────────────────────────────────────────────


def run_dry(
    orgs: list[dict],
    *,
    api_key: str,
    rate_limiter: BraveRateLimiter,
    search_parallelism: int = 4,
    fetch_parallelism: int = 8,
) -> None:
    """Search + fetch without calling Gemma or writing to DB (AC10)."""
    for org in orgs:
        ein = org["ein"]
        name = org.get("name", "")
        city = org.get("city", "")
        state = org.get("state", "")

        try:
            results = search_and_filter(
                name, city, state,
                api_key=api_key,
                rate_limiter=rate_limiter,
            )
        except BraveSearchError as exc:
            print(f"SEARCH ERROR ein={ein}: {exc}")
            continue

        if not results:
            print(f"NO RESULTS ein={ein} name={name}")
            continue

        candidates = []
        with ThreadPoolExecutor(max_workers=fetch_parallelism) as pool:
            futures = {
                pool.submit(_fetch_candidate, r.url): r
                for r in results
            }
            for future in as_completed(futures):
                brave_result = futures[future]
                cand = future.result()
                cand["title"] = brave_result.title
                cand["snippet"] = brave_result.snippet
                candidates.append(cand)

        live = [c for c in candidates if c["live"]]
        print(
            f"ein={ein} name={name[:40]} "
            f"search_results={len(results)} live={len(live)}"
        )
        for c in live:
            print(f"  {c['final_url']} ({c['excerpt'][:80]}...)")


__all__ = [
    "ConsumerStats",
    "PipelineQueue",
    "ProducerStats",
    "ShutdownFlag",
    "consumer",
    "install_sigint_handler",
    "load_unresolved_orgs",
    "producer",
    "run_dry",
]
