"""SQLite schema for spec 0004.

Matches the Data Schema block in `locard/specs/0004-*.md` verbatim — the
reports table, fetch_log, crawled_orgs, deletion_log, budget_ledger, and
the `reports_public` view with its 3-filter WHERE clause (attribution,
classification confidence, active-content).

Writes go through `?`-parameterized queries only (see db_writer.py); the
`insert_raw_report_for_test` helper is explicitly scoped to tests.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import config


# NOTE: `reports_public`'s WHERE clause is INTENTIONALLY formatted in a
# way `test_ac26_*` can grep for: the three filter clauses are each on
# their own logical line with the exact substrings the tests match.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reports (
  content_sha256       TEXT PRIMARY KEY,

  source_url_redacted         TEXT NOT NULL,
  referring_page_url_redacted TEXT,
  redirect_chain_json         TEXT,
  source_org_ein              TEXT NOT NULL,
  discovered_via              TEXT NOT NULL,
  hosting_platform            TEXT,

  attribution_confidence TEXT NOT NULL,

  archived_at     TEXT NOT NULL,
  content_type    TEXT NOT NULL,
  file_size_bytes INTEGER NOT NULL,
  page_count      INTEGER,

  first_page_text   TEXT,
  pdf_creator       TEXT,
  pdf_producer      TEXT,
  pdf_creation_date TEXT,

  pdf_has_javascript  INTEGER NOT NULL DEFAULT 0,
  pdf_has_launch      INTEGER NOT NULL DEFAULT 0,
  pdf_has_embedded    INTEGER NOT NULL DEFAULT 0,
  pdf_has_uri_actions INTEGER NOT NULL DEFAULT 0,

  classification            TEXT,
  classification_confidence REAL,
  classifier_model          TEXT NOT NULL,
  classifier_version        INTEGER NOT NULL DEFAULT 1,
  classified_at             TEXT,

  report_year        INTEGER,
  report_year_source TEXT,

  extractor_version INTEGER NOT NULL DEFAULT 1,

  CHECK (length(content_sha256) = 64),
  CHECK (file_size_bytes > 0),
  CHECK (content_type = 'application/pdf'),
  CHECK (discovered_via IN ('sitemap','homepage-link','subpage-link','hosting-platform')),
  CHECK (hosting_platform IS NULL OR hosting_platform IN
         ('issuu','flipsnack','canva','own-domain','own-cms')),
  CHECK (classification IS NULL OR classification IN
         ('annual','impact','hybrid','other','not_a_report')),
  CHECK (classification_confidence IS NULL OR
         (classification_confidence >= 0 AND classification_confidence <= 1)),
  CHECK (attribution_confidence IN ('own_domain','platform_verified','platform_unverified')),
  CHECK (redirect_chain_json IS NULL OR length(redirect_chain_json) <= 2048),
  CHECK (pdf_has_javascript IN (0,1)),
  CHECK (pdf_has_launch IN (0,1)),
  CHECK (pdf_has_embedded IN (0,1)),
  CHECK (pdf_has_uri_actions IN (0,1)),
  CHECK (first_page_text IS NULL OR length(first_page_text) <= 4096),
  CHECK (pdf_creator IS NULL OR length(pdf_creator) <= 200),
  CHECK (pdf_producer IS NULL OR length(pdf_producer) <= 200),
  CHECK (report_year_source IS NULL OR report_year_source IN
         ('url','filename','first-page','pdf-creation-date'))
);
CREATE INDEX IF NOT EXISTS idx_reports_ein            ON reports(source_org_ein);
CREATE INDEX IF NOT EXISTS idx_reports_classification ON reports(classification);
CREATE INDEX IF NOT EXISTS idx_reports_year           ON reports(report_year);
CREATE INDEX IF NOT EXISTS idx_reports_platform       ON reports(hosting_platform);

CREATE VIEW IF NOT EXISTS reports_public AS
  SELECT content_sha256, source_org_ein, hosting_platform,
         attribution_confidence,
         archived_at, file_size_bytes, page_count,
         classification, classification_confidence,
         report_year, report_year_source,
         pdf_has_javascript, pdf_has_launch, pdf_has_embedded
  FROM reports
  WHERE attribution_confidence IN ('own_domain','platform_verified')
    AND classification IS NOT NULL
    AND classification_confidence >= 0.8
    AND pdf_has_javascript = 0
    AND pdf_has_launch = 0
    AND pdf_has_embedded = 0;

CREATE TABLE IF NOT EXISTS fetch_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ein            TEXT,
  url_redacted   TEXT NOT NULL,
  kind           TEXT NOT NULL,
  fetch_status   TEXT NOT NULL,
  status_code    INTEGER,
  fetched_at     TEXT NOT NULL,
  elapsed_ms     INTEGER,
  notes          TEXT,
  CHECK (kind IN ('robots','sitemap','homepage','subpage','pdf-head','pdf-get','classify')),
  CHECK (fetch_status IN ('ok','not_found','rate_limited','forbidden','server_error',
                          'network_error','size_capped','blocked_content_type',
                          'blocked_scheme','blocked_ssrf','cross_origin_blocked',
                          'blocked_robots','classifier_error'))
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_ein ON fetch_log(ein);

CREATE TABLE IF NOT EXISTS crawled_orgs (
  ein                    TEXT PRIMARY KEY,
  first_crawled_at       TEXT NOT NULL,
  last_crawled_at        TEXT NOT NULL,
  candidate_count        INTEGER NOT NULL DEFAULT 0,
  fetched_count          INTEGER NOT NULL DEFAULT 0,
  confirmed_report_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deletion_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  content_sha256  TEXT NOT NULL,
  deleted_at      TEXT NOT NULL,
  reason          TEXT,
  operator        TEXT,
  pdf_unlinked    INTEGER NOT NULL,
  CHECK (pdf_unlinked IN (0,1))
);

CREATE TABLE IF NOT EXISTS budget_ledger (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  at_timestamp      TEXT NOT NULL,
  classifier_model  TEXT NOT NULL,
  sha256_classified TEXT NOT NULL,
  input_tokens      INTEGER NOT NULL,
  output_tokens     INTEGER NOT NULL,
  cents_spent       INTEGER NOT NULL,
  notes             TEXT,
  CHECK (cents_spent >= 0),
  CHECK (input_tokens >= 0),
  CHECK (output_tokens >= 0),
  CHECK (length(sha256_classified) = 64 OR sha256_classified = 'preflight')
);
CREATE INDEX IF NOT EXISTS idx_budget_ledger_at ON budget_ledger(at_timestamp);
"""


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    newly_created = not path.exists()
    conn = sqlite3.connect(
        str(path),
        timeout=30,
        isolation_level=None,  # autocommit; explicit BEGIN/COMMIT in writers
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if read_only:
        conn.execute("PRAGMA query_only = 1")
    if newly_created:
        try:
            os.chmod(path, config.DB_MODE)
        except OSError:
            pass
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def ensure_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    init_schema(conn)
    try:
        os.chmod(db_path, config.DB_MODE)
    except OSError:
        pass
    return conn


def insert_raw_report_for_test(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
    source_org_ein: str,
    attribution_confidence: str = "own_domain",
    classification: str | None = "annual",
    classification_confidence: float | None = 0.9,
    report_year: int | None = 2024,
    archived_at: str | None = None,
    pdf_has_javascript: int = 0,
    pdf_has_launch: int = 0,
    pdf_has_embedded: int = 0,
    pdf_has_uri_actions: int = 0,
    hosting_platform: str | None = None,
    discovered_via: str = "homepage-link",
    classifier_model: str = "claude-haiku-4-5",
    file_size_bytes: int = 1024,
) -> None:
    """Test-only helper: insert a pre-shaped row into `reports`.

    Production writes go through `db_writer.py` with parameter binding +
    validation; this helper exists solely to let tests drive the public
    view and catalogue queries without rebuilding the full pipeline.
    """
    if archived_at is None:
        archived_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO reports (
          content_sha256, source_url_redacted, source_org_ein, discovered_via,
          hosting_platform, attribution_confidence, archived_at, content_type,
          file_size_bytes, classification, classification_confidence,
          classifier_model, pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
          pdf_has_uri_actions, report_year
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            content_sha256,
            f"https://example.org/report/{content_sha256[:8]}.pdf",
            source_org_ein,
            discovered_via,
            hosting_platform,
            attribution_confidence,
            archived_at,
            "application/pdf",
            file_size_bytes,
            classification,
            classification_confidence,
            classifier_model,
            pdf_has_javascript,
            pdf_has_launch,
            pdf_has_embedded,
            pdf_has_uri_actions,
            report_year,
        ),
    )


__all__ = [
    "SCHEMA_SQL",
    "connect",
    "init_schema",
    "ensure_db",
    "insert_raw_report_for_test",
]
