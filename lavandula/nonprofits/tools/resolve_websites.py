"""Brave-based website resolver for nonprofit seeds.

Queries the Brave Search API to fill website_url for rows in nonprofits_seed
that currently have website_url IS NULL.

Usage:
    python -m lavandula.nonprofits.tools.resolve_websites [OPTIONS]

Options:
    --db PATH      Path to seeds.db (default: <package>/data/seeds.db)
    --limit N      Stop after N lookups; 0 = no limit
    --qps FLOAT    Queries per second cap (default: 1.0)
    --dry-run      Run queries, print chosen URLs, do NOT write to DB
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from lavandula.common.secrets import SecretUnavailable, get_brave_api_key

log = logging.getLogger(__name__)

# ── Blocklist ─────────────────────────────────────────────────────────────────

BLOCKLIST_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
    "guidestar.org", "propublica.org", "charitynavigator.org",
    "idealist.org", "causeiq.com", "dnb.com", "yelp.com",
    "rocketreach.co", "candid.org", "give.org", "benevity.org",
    "mapquest.com", "chamberofcommerce.com", "zoominfo.com",
    "crunchbase.com", "bloomberg.com", "reddit.com",
    "wikipedia.org", "greatnonprofits.org", "theorg.com",
    "govtribe.com", "wellness.com", "givefreely.com",
    "whereorg.com", "influencewatch.org", "foundationcenter.org",
    "fconline.foundationcenter.org", "intellispect.co", "gudsy.org",
    "nursa.com", "app.milliegiving.com", "milliegiving.com",
    "npidb.org",
})

SHORTENER_HOSTS = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly",
    "rebrand.ly", "shorturl.at", "lnkd.in",
})

RESOLVER_ACCEPT_THRESHOLD = 0.85
RESOLVER_AMBIGUOUS_THRESHOLD = 0.55
RESOLVER_AMBIGUOUS_DELTA = 0.10


def _is_blocklisted(host: str) -> bool:
    host = host.lower()
    if host.endswith(".gov") or host.endswith(".mil"):
        return True
    if host in BLOCKLIST_HOSTS:
        return True
    for bad in BLOCKLIST_HOSTS:
        if host.endswith("." + bad):
            return True
    return False


def _is_shortener(host: str) -> bool:
    host = host.lower()
    if host in SHORTENER_HOSTS:
        return True
    return any(host.endswith("." + bad) for bad in SHORTENER_HOSTS)


# ── URL validation ────────────────────────────────────────────────────────────

def _validate_url(url: str) -> str | None:
    """Return canonical scheme://host or None if the URL fails validation."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except Exception:
        return None

    if parts.scheme not in ("http", "https"):
        return None

    hostname = parts.hostname  # lowercase, port-stripped by urlsplit
    if not hostname:
        return None

    if "." not in hostname:
        return None

    if "xn--" in hostname:
        return None

    try:
        hostname.encode("ascii")
    except UnicodeEncodeError:
        return None

    if parts.username is not None:
        return None

    try:
        ipaddress.ip_address(hostname)
        return None  # raw IP — reject
    except ValueError:
        pass

    return f"{parts.scheme}://{hostname}"


def _apply_migrations(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(nonprofits_seed)")}
    for col, typedef in [
        ("notes", "TEXT DEFAULT NULL"),
        ("resolver_confidence", "REAL DEFAULT NULL"),
        ("resolver_status", "TEXT DEFAULT NULL"),
        ("resolver_method", "TEXT DEFAULT NULL"),
        ("resolver_reason", "TEXT DEFAULT NULL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE nonprofits_seed ADD COLUMN {col} {typedef}")
    conn.commit()


def _org_tokens(name: str) -> list[str]:
    stop = {
        "the", "for", "and", "inc", "foundation", "center", "services",
        "service", "association", "school", "academy", "hospital",
        "health", "community", "of", "in", "at", "on", "llc", "corp",
        "corporation", "co", "group",
    }
    tokens = []
    for raw in "".join(ch if ch.isalnum() else " " for ch in (name or "")).lower().split():
        if len(raw) < 4 or raw in stop:
            continue
        tokens.append(raw)
    return tokens[:6]


def _hostname_quality(host: str) -> float:
    host = host.lower()
    penalties = ("planmygift", "donors", "fundraise", "donate", "business.", "app.")
    if any(p in host for p in penalties):
        return 0.15
    if host.count(".") == 1:
        return 0.90
    if host.startswith("www."):
        return 0.88
    return 0.82


def _score_candidate(result: dict, *, name: str, city: str | None) -> tuple[float, str | None, str]:
    """Return (score, canonical_url, reason)."""
    url = result.get("url") or ""
    canonical = _validate_url(url)
    if canonical is None:
        return 0.0, None, "invalid_url"
    host = urlsplit(canonical).hostname or ""
    if _is_shortener(host):
        return 0.0, None, "blocked_shortener"
    if _is_blocklisted(host):
        return 0.0, None, "blocked_host"

    title = (result.get("title") or "").lower()
    desc = (result.get("description") or result.get("snippet") or "").lower()
    path = (urlsplit(url).path or "").lower()
    text = f"{title} {desc}"
    tokens = _org_tokens(name)
    city_token = (city or "").lower().strip()

    score = _hostname_quality(host)
    matched_tokens = 0
    for tok in tokens:
        if tok in host.replace("-", ""):
            score += 0.18
            matched_tokens += 1
        elif tok in text:
            score += 0.12
            matched_tokens += 1
    if matched_tokens >= 2:
        score += 0.08
    if city_token and city_token in text:
        score += 0.08
    if path and path not in ("", "/"):
        depth = len([seg for seg in path.split("/") if seg])
        if depth >= 2:
            score -= 0.08
    if any(seg in path for seg in ("/donate", "/fund", "/directory", "/profile", "/people")):
        score -= 0.20
    score = max(0.0, min(1.0, score))
    return score, canonical, f"score={score:.2f};matched_tokens={matched_tokens};host={host}"


def _pick_best(results: list[dict], *, name: str, city: str | None) -> tuple[str | None, float | None, str, str]:
    """Return (chosen_url, confidence, status, reason)."""
    scored: list[tuple[float, str, str]] = []
    last_reason = "no-valid-result"
    for result in results:
        score, canonical, reason = _score_candidate(result, name=name, city=city)
        if canonical is None:
            last_reason = reason
            continue
        scored.append((score, canonical, reason))
    if not scored:
        return None, None, "rejected", last_reason

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_url, best_reason = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_score >= RESOLVER_ACCEPT_THRESHOLD and (best_score - second_score >= RESOLVER_AMBIGUOUS_DELTA):
        return best_url, best_score, "accepted", best_reason
    if best_score >= RESOLVER_AMBIGUOUS_THRESHOLD:
        return None, best_score, "ambiguous", f"ambiguous_top_two={best_score:.2f}/{second_score:.2f}"
    return None, best_score, "rejected", best_reason


def _pick_primary(results: list[dict]) -> str | None:
    """Compatibility wrapper used by older tests."""
    for result in results:
        url = result.get("url") or ""
        canonical = _validate_url(url)
        if canonical is None:
            continue
        host = urlsplit(canonical).hostname or ""
        if _is_blocklisted(host) or _is_shortener(host):
            continue
        return canonical
    return None


# ── Brave API client ──────────────────────────────────────────────────────────

_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


def _brave_search(query: str, *, key: str) -> dict:
    r = requests.get(
        _BRAVE_URL,
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": query, "count": 10, "safesearch": "moderate"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Retry logic ───────────────────────────────────────────────────────────────

def _search_with_retry(
    query: str,
    *,
    key: str,
    log: logging.Logger,
    brave_search_fn=_brave_search,
) -> tuple[dict | None, str | None]:
    """Return (response_or_None, error_note_or_None)."""
    last_note = "brave_error:unknown"
    for attempt in (1, 2):
        try:
            return brave_search_fn(query, key=key), None
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            last_note = f"brave_error:{code}"
            log.warning("brave_error status=%s attempt=%d", code, attempt)
            if attempt == 1:
                time.sleep(30)
        except Exception as exc:
            last_note = f"brave_error:{type(exc).__name__}"
            log.warning("brave_error type=%s attempt=%d", type(exc).__name__, attempt)
            if attempt == 1:
                time.sleep(30)
    return None, last_note


# ── Main resolution loop ──────────────────────────────────────────────────────

def resolve_batch(
    conn: sqlite3.Connection,
    *,
    key: str,
    limit: int,
    min_sleep: float,
    dry_run: bool,
    log: logging.Logger,
    _search_fn=None,
) -> None:
    """Iterate rows with NULL website_url, query Brave, write results."""
    _apply_migrations(conn)
    rows = conn.execute(
        "SELECT ein, name, city FROM nonprofits_seed WHERE website_url IS NULL"
        + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()

    for ein, name, city in rows:
        safe_name = (name or "").replace('"', "")
        safe_city = (city or "").replace('"', "") if city else None

        query = (
            f'"{safe_name}" {safe_city} nonprofit official website'
            if safe_city
            else f'"{safe_name}" nonprofit official website'
        )

        t0 = time.monotonic()
        search_fn = _search_fn if _search_fn is not None else _brave_search
        response, error_note = _search_with_retry(query, key=key, log=log, brave_search_fn=search_fn)
        elapsed = time.monotonic() - t0

        if response is None:
            notes = error_note
            chosen = None
            confidence = None
            resolver_status = "error"
            resolver_reason = error_note or "brave_error:unknown"
        else:
            results = (response.get("web") or {}).get("results") or []
            chosen, confidence, resolver_status, resolver_reason = _pick_best(
                results,
                name=safe_name,
                city=safe_city,
            )
            if chosen is None and confidence is None and resolver_status == "rejected":
                resolver_reason = "no-non-blocklist-result"
            notes = None if chosen else resolver_reason

        # Structural truncation to first 3 results keeps JSON valid and bounded.
        if response:
            audit_data = dict(response)
            if "web" in audit_data and "results" in (audit_data.get("web") or {}):
                audit_data["web"] = dict(audit_data["web"])
                audit_data["web"]["results"] = audit_data["web"]["results"][:3]
            candidates_json = json.dumps(audit_data)
        else:
            candidates_json = None

        log.info("org ein=%s name=%s url=%s", ein, (name or "")[:40], chosen)

        if dry_run:
            print(f"DRY-RUN ein={ein} url={chosen}")
        else:
            conn.execute(
                "UPDATE nonprofits_seed"
                " SET website_url=?, website_candidates_json=?, notes=?,"
                " resolver_confidence=?, resolver_status=?, resolver_method=?, resolver_reason=?"
                " WHERE ein=?",
                (
                    chosen,
                    candidates_json,
                    notes,
                    confidence,
                    resolver_status,
                    "brave-scored",
                    resolver_reason,
                    ein,
                ),
            )
            conn.commit()

        to_sleep = min_sleep - elapsed
        if to_sleep > 0:
            time.sleep(to_sleep)


# ── CLI ───────────────────────────────────────────────────────────────────────

_DEFAULT_DB = Path(__file__).parent.parent / "data" / "seeds.db"


def _resolve_llm_batch(
    conn: sqlite3.Connection,
    *,
    max_orgs: int,
    dry_run: bool,
) -> None:
    """Resolve orgs using the LLM-backed resolver (Spec 0005)."""
    from lavandula.nonprofits.resolver_clients import (
        OrgIdentity,
        make_resolver_http_client,
        select_resolver_client,
    )

    _apply_migrations(conn)
    client = select_resolver_client()
    http_client = make_resolver_http_client()

    rows = conn.execute(
        "SELECT ein, name, address, city, state, zipcode, ntee_code"
        " FROM nonprofits_seed WHERE website_url IS NULL"
        + (" LIMIT ?" if max_orgs > 0 else ""),
        (max_orgs,) if max_orgs > 0 else (),
    ).fetchall()

    for ein, name, address, city, state, zipcode, ntee_code in rows:
        org = OrgIdentity(
            ein=ein,
            name=name or "",
            address=address or None,
            city=city or "",
            state=state or "",
            zipcode=zipcode or None,
            ntee_code=ntee_code or None,
        )
        result = client.resolve(org, http_client)
        log.info(
            "org ein=%s name=%s status=%s url=%s",
            ein,
            (name or "")[:40],
            result.status,
            result.url,
        )
        if dry_run:
            print(f"DRY-RUN ein={ein} status={result.status} url={result.url}")
        else:
            conn.execute(
                "UPDATE nonprofits_seed SET"
                " website_url=?,"
                " resolver_status=?,"
                " resolver_confidence=?,"
                " resolver_method=?,"
                " resolver_reason=?,"
                " website_candidates_json=?"
                " WHERE ein=?",
                (
                    result.url,
                    result.status,
                    result.confidence,
                    result.method,
                    result.reason,
                    json.dumps(result.candidates),
                    ein,
                ),
            )
            conn.commit()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="resolve_websites",
        description="Fill website_url for nonprofit seeds via Brave Search API.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        metavar="PATH",
        help="Path to seeds.db (default: %(default)s)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Stop after N lookups; 0 = no limit (default: 0)",
    )
    p.add_argument(
        "--qps",
        type=float,
        default=1.0,
        metavar="FLOAT",
        help="Queries per second cap (default: 1.0)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print chosen URLs without writing to DB",
    )
    p.add_argument(
        "--resolver",
        choices=("heuristic", "llm"),
        default="heuristic",
        help="Resolution strategy: 'heuristic' (default) or 'llm' (DeepSeek/Qwen)",
    )
    p.add_argument(
        "--max-orgs",
        type=int,
        default=50,
        metavar="N",
        help="Max orgs to process per run when --resolver llm (default: 50)",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.qps <= 0:
        parser.error("--qps must be > 0")
    if args.limit < 0:
        parser.error("--limit must be >= 0")

    conn = sqlite3.connect(str(args.db))
    try:
        if args.resolver == "llm":
            _resolve_llm_batch(
                conn,
                max_orgs=args.max_orgs,
                dry_run=args.dry_run,
            )
        else:
            try:
                key = get_brave_api_key()
            except SecretUnavailable as exc:
                log.error("brave API key unavailable: %s", exc)
                sys.exit(1)
            min_sleep = 1.0 / args.qps
            resolve_batch(
                conn,
                key=key,
                limit=args.limit,
                min_sleep=min_sleep,
                dry_run=args.dry_run,
                log=log,
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
