"""Parameterized writes into the spec-0004 schema (AC23 compliance).

Every SQL write goes through `?` parameter binding. This module plus
`catalogue.py` and `schema.py` are the ONLY files permitted to reference
the `reports` table directly (AC23); everything else reads through the
`reports_public` view.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def record_fetch(
    conn: sqlite3.Connection,
    *,
    ein: str | None,
    url_redacted: str,
    kind: str,
    fetch_status: str,
    status_code: int | None = None,
    elapsed_ms: int | None = None,
    notes: str | None = None,
) -> None:
    """Append a row to fetch_log."""
    conn.execute(
        """
        INSERT INTO fetch_log
          (ein, url_redacted, kind, fetch_status, status_code, fetched_at,
           elapsed_ms, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ein, url_redacted, kind, fetch_status, status_code, _now_iso(),
         elapsed_ms, notes),
    )


def upsert_crawled_org(
    conn: sqlite3.Connection,
    *,
    ein: str,
    candidate_count: int,
    fetched_count: int,
    confirmed_report_count: int,
) -> None:
    """Track that this EIN has been processed (AC20 resume)."""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO crawled_orgs
          (ein, first_crawled_at, last_crawled_at, candidate_count,
           fetched_count, confirmed_report_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ein) DO UPDATE SET
          last_crawled_at = excluded.last_crawled_at,
          candidate_count = excluded.candidate_count,
          fetched_count = excluded.fetched_count,
          confirmed_report_count = excluded.confirmed_report_count
        """,
        (ein, now, now, candidate_count, fetched_count,
         confirmed_report_count),
    )


def upsert_report(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
    source_url_redacted: str,
    referring_page_url_redacted: str | None,
    redirect_chain_redacted: list[str] | None,
    source_org_ein: str,
    discovered_via: str,
    hosting_platform: str | None,
    attribution_confidence: str,
    content_type: str = "application/pdf",
    file_size_bytes: int,
    page_count: int | None,
    first_page_text: str | None,
    pdf_creator: str | None,
    pdf_producer: str | None,
    pdf_creation_date: str | None,
    pdf_has_javascript: int,
    pdf_has_launch: int,
    pdf_has_embedded: int,
    pdf_has_uri_actions: int,
    classification: str | None,
    classification_confidence: float | None,
    classifier_model: str,
    classifier_version: int,
    report_year: int | None,
    report_year_source: str | None,
    extractor_version: int,
) -> None:
    """Insert a fully-shaped row into reports. Idempotent on sha."""
    chain_json = (
        json.dumps(redirect_chain_redacted, ensure_ascii=False)[:2048]
        if redirect_chain_redacted
        else None
    )
    classified_at = _now_iso() if classification is not None else None
    archived_at = _now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO reports (
          content_sha256, source_url_redacted, referring_page_url_redacted,
          redirect_chain_json, source_org_ein, discovered_via,
          hosting_platform, attribution_confidence, archived_at,
          content_type, file_size_bytes, page_count,
          first_page_text, pdf_creator, pdf_producer, pdf_creation_date,
          pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
          pdf_has_uri_actions, classification, classification_confidence,
          classifier_model, classifier_version, classified_at,
          report_year, report_year_source, extractor_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            content_sha256, source_url_redacted, referring_page_url_redacted,
            chain_json, source_org_ein, discovered_via,
            hosting_platform, attribution_confidence, archived_at,
            content_type, file_size_bytes, page_count,
            first_page_text, pdf_creator, pdf_producer, pdf_creation_date,
            pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
            pdf_has_uri_actions, classification, classification_confidence,
            classifier_model, classifier_version, classified_at,
            report_year, report_year_source, extractor_version,
        ),
    )


def record_deletion(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
    reason: str | None,
    operator: str | None,
    pdf_unlinked: int,
) -> None:
    conn.execute(
        """
        INSERT INTO deletion_log
          (content_sha256, deleted_at, reason, operator, pdf_unlinked)
        VALUES (?, ?, ?, ?, ?)
        """,
        (content_sha256, _now_iso(), reason, operator, pdf_unlinked),
    )


__all__ = [
    "record_fetch",
    "upsert_crawled_org",
    "upsert_report",
    "record_deletion",
]
