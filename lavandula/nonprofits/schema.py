"""SQLite schema for the nonprofit seed list DB.

The schema matches spec 0001's Data Schema section verbatim. All writes go
through `?`-parameterized queries (see db_writer.py); direct string concat
is a review defect.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nonprofits (
  ein                TEXT PRIMARY KEY,
  name               TEXT NOT NULL,
  website_url        TEXT,
  website_url_raw    TEXT,

  rating_stars       INTEGER,
  overall_score      REAL,
  beacons_completed  INTEGER,
  rated              INTEGER NOT NULL DEFAULT 0,

  total_revenue      INTEGER,
  total_expenses     INTEGER,
  program_expense_pct REAL,

  ntee_major         TEXT,
  ntee_code          TEXT,
  cn_cause           TEXT,

  city               TEXT,
  state              TEXT,
  address            TEXT,

  mission            TEXT,

  cn_profile_url     TEXT NOT NULL,

  redirected_to_ein  TEXT,
  parse_status       TEXT NOT NULL DEFAULT 'ok',
  website_url_reason TEXT,

  last_fetched_at    TEXT NOT NULL,
  content_sha256     TEXT NOT NULL,
  parse_version      INTEGER NOT NULL DEFAULT 1,

  CHECK (length(ein) = 9),
  CHECK (rating_stars IS NULL OR rating_stars BETWEEN 1 AND 4),
  CHECK (beacons_completed IS NULL OR beacons_completed BETWEEN 0 AND 4),
  CHECK (overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)),
  CHECK (parse_status IN ('ok','partial','blocked','challenge','unparsed')),
  CHECK (website_url_reason IS NULL OR website_url_reason IN
         ('missing','mailto','tel','social','unwrap_failed','invalid'))
);
CREATE INDEX IF NOT EXISTS idx_nonprofits_state        ON nonprofits(state);
CREATE INDEX IF NOT EXISTS idx_nonprofits_rating_stars ON nonprofits(rating_stars);
CREATE INDEX IF NOT EXISTS idx_nonprofits_ntee_major   ON nonprofits(ntee_major);
CREATE INDEX IF NOT EXISTS idx_nonprofits_revenue      ON nonprofits(total_revenue);
CREATE INDEX IF NOT EXISTS idx_nonprofits_parse_status ON nonprofits(parse_status);

CREATE TABLE IF NOT EXISTS fetch_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ein            TEXT,
  url            TEXT NOT NULL,
  status_code    INTEGER,
  attempt        INTEGER NOT NULL,
  is_retry       INTEGER NOT NULL DEFAULT 0,
  fetch_status   TEXT NOT NULL,
  fetched_at     TEXT NOT NULL,
  elapsed_ms     INTEGER,
  bytes_read     INTEGER,
  notes          TEXT,
  error          TEXT,
  CHECK (fetch_status IN
    ('ok','not_found','rate_limited','forbidden','challenge',
     'server_error','network_error','size_capped','disallowed_by_robots'))
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_ein      ON fetch_log(ein);
CREATE INDEX IF NOT EXISTS idx_fetch_log_status   ON fetch_log(fetch_status);
CREATE INDEX IF NOT EXISTS idx_fetch_log_is_retry ON fetch_log(is_retry);

CREATE TABLE IF NOT EXISTS sitemap_entries (
  ein            TEXT PRIMARY KEY,
  source_sitemap TEXT NOT NULL,
  first_seen_at  TEXT NOT NULL,
  lastmod        TEXT
);
"""


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with pragmas suited to this workload.

    WAL journaling + NORMAL sync lets the crawler write quickly while
    staying crash-safe. read_only=True sets PRAGMA query_only for the
    report.py path.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    newly_created = not path.exists()
    conn = sqlite3.connect(
        str(path),
        timeout=30,
        isolation_level=None,  # autocommit; explicit BEGIN/COMMIT in writers
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if read_only:
        conn.execute("PRAGMA query_only = 1")
    if newly_created:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def ensure_db(db_path: Path | str) -> sqlite3.Connection:
    """Open + initialize the schema. Idempotent."""
    conn = connect(db_path)
    init_schema(conn)
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn
