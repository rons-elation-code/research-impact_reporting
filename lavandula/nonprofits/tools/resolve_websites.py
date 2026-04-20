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
    "wikipedia.org",
})


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


def _pick_primary(results: list[dict]) -> str | None:
    """Return first valid, non-blocklisted canonical URL from Brave results."""
    for result in results:
        url = result.get("url") or ""
        canonical = _validate_url(url)
        if canonical is None:
            continue
        host = urlsplit(canonical).hostname or ""
        if _is_blocklisted(host):
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
        else:
            results = (response.get("web") or {}).get("results") or []
            chosen = _pick_primary(results)
            notes = None if chosen else "no-non-blocklist-result"

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
                " SET website_url=?, website_candidates_json=?, notes=?"
                " WHERE ein=?",
                (chosen, candidates_json, notes, ein),
            )
            conn.commit()

        to_sleep = min_sleep - elapsed
        if to_sleep > 0:
            time.sleep(to_sleep)


# ── CLI ───────────────────────────────────────────────────────────────────────

_DEFAULT_DB = Path(__file__).parent.parent / "data" / "seeds.db"


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

    try:
        key = get_brave_api_key()
    except SecretUnavailable as exc:
        log.error("brave API key unavailable: %s", exc)
        sys.exit(1)

    conn = sqlite3.connect(str(args.db))
    min_sleep = 1.0 / args.qps
    try:
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
