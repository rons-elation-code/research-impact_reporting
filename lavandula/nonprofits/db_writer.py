"""Parameterized SQLite writes. No string-concatenated SQL anywhere.

Each function opens its own transaction; callers may opt in to a larger
BEGIN/COMMIT scope by passing an existing connection.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import asdict
from typing import Any

from .extract import ExtractedProfile
from .logging_utils import sanitize


_NONPROFIT_COLS = (
    "ein", "name", "website_url", "website_url_raw",
    "rating_stars", "overall_score", "beacons_completed", "rated",
    "total_revenue", "total_expenses", "program_expense_pct",
    "ntee_major", "ntee_code", "cn_cause",
    "city", "state", "address",
    "mission", "cn_profile_url",
    "redirected_to_ein", "parse_status", "website_url_reason",
    "last_fetched_at", "content_sha256", "parse_version",
)


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def upsert_nonprofit(
    conn: sqlite3.Connection,
    profile: ExtractedProfile,
    *,
    cn_profile_url: str,
    content_sha256: str,
    last_fetched_at: str | None = None,
    redirected_to_ein: str | None = None,
    parse_version: int = 1,
) -> None:
    """Insert-or-replace one nonprofit row, fully parameterized."""
    values = {
        "ein": profile.ein,
        "name": sanitize(profile.name, max_len=500),
        "website_url": profile.website_url,
        "website_url_raw": sanitize(profile.website_url_raw, max_len=2048) or None,
        "rating_stars": profile.rating_stars,
        "overall_score": profile.overall_score,
        "beacons_completed": profile.beacons_completed,
        "rated": profile.rated,
        "total_revenue": profile.total_revenue,
        "total_expenses": profile.total_expenses,
        "program_expense_pct": profile.program_expense_pct,
        "ntee_major": profile.ntee_major,
        "ntee_code": profile.ntee_code,
        "cn_cause": sanitize(profile.cn_cause, max_len=500) or None,
        "city": sanitize(profile.city, max_len=200) or None,
        "state": profile.state,
        "address": sanitize(profile.address, max_len=500) or None,
        "mission": profile.mission,  # intentionally not truncated — legit copy
        "cn_profile_url": cn_profile_url,
        "redirected_to_ein": redirected_to_ein,
        "parse_status": profile.parse_status,
        "website_url_reason": profile.website_url_reason,
        "last_fetched_at": last_fetched_at or _iso_now(),
        "content_sha256": content_sha256,
        "parse_version": parse_version,
    }
    cols = ", ".join(_NONPROFIT_COLS)
    placeholders = ", ".join(f":{c}" for c in _NONPROFIT_COLS)
    sql = f"INSERT OR REPLACE INTO nonprofits ({cols}) VALUES ({placeholders})"
    conn.execute(sql, values)


def insert_fetch_log(
    conn: sqlite3.Connection,
    *,
    ein: str | None,
    url: str,
    status_code: int | None,
    attempt: int,
    is_retry: bool,
    fetch_status: str,
    elapsed_ms: int | None,
    bytes_read: int | None,
    notes: str | None = None,
    error: str | None = None,
    fetched_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_log
          (ein, url, status_code, attempt, is_retry, fetch_status,
           fetched_at, elapsed_ms, bytes_read, notes, error)
        VALUES (:ein, :url, :status_code, :attempt, :is_retry, :fetch_status,
                :fetched_at, :elapsed_ms, :bytes_read, :notes, :error)
        """,
        {
            "ein": ein,
            "url": sanitize(url, max_len=2048),
            "status_code": status_code,
            "attempt": attempt,
            "is_retry": 1 if is_retry else 0,
            "fetch_status": fetch_status,
            "fetched_at": fetched_at or _iso_now(),
            "elapsed_ms": elapsed_ms,
            "bytes_read": bytes_read,
            "notes": sanitize(notes) or None,
            "error": sanitize(error) or None,
        },
    )


def insert_sitemap_entry(
    conn: sqlite3.Connection,
    *,
    ein: str,
    source_sitemap: str,
    first_seen_at: str | None = None,
    lastmod: str | None = None,
) -> None:
    """First-seen precedence via INSERT OR IGNORE."""
    conn.execute(
        """
        INSERT OR IGNORE INTO sitemap_entries (ein, source_sitemap, first_seen_at, lastmod)
        VALUES (:ein, :source_sitemap, :first_seen_at, :lastmod)
        """,
        {
            "ein": ein,
            "source_sitemap": sanitize(source_sitemap, max_len=200),
            "first_seen_at": first_seen_at or _iso_now(),
            "lastmod": lastmod,
        },
    )


def fetched_eins(conn: sqlite3.Connection) -> set[str]:
    """Set of EINs already present in `nonprofits` (i.e., already fetched).

    Used by the crawler to support checkpoint-less resume.
    """
    rows = conn.execute("SELECT ein FROM nonprofits").fetchall()
    return {r[0] for r in rows}


def unfetched_sitemap_entries(conn: sqlite3.Connection, *, limit: int | None = None):
    """Yield (ein, source_sitemap, lastmod) for EINs not yet in nonprofits."""
    base = """
    SELECT s.ein, s.source_sitemap, s.lastmod
    FROM sitemap_entries s
    LEFT JOIN nonprofits n ON n.ein = s.ein
    WHERE n.ein IS NULL
    ORDER BY s.first_seen_at, s.ein
    """
    if limit is not None:
        base += " LIMIT :limit"
        rows = conn.execute(base, {"limit": int(limit)}).fetchall()
    else:
        rows = conn.execute(base).fetchall()
    for r in rows:
        yield r[0], r[1], r[2]
