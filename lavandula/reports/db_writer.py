"""Parameterized writes into the `lava_impact` schema (Spec 0017).

All writes go through SQLAlchemy `text()` with `:named` bind parameters
against a single SQLAlchemy engine. Every public function takes an
`engine` and wraps its work in `engine.begin()` so the transaction
commits at block exit (or rolls back on exception).

The attribution-rank merge logic that SQLite implemented as a
read-then-write is expressed here as an atomic
`INSERT ... ON CONFLICT (content_sha256) DO UPDATE SET ...` using the
`lava_impact.attribution_rank(TEXT)` helper function from migration
`002_attribution_helper.sql`.

This module plus `catalogue.py` and `schema.py` are the only files
permitted to reference the `lava_impact.reports` table directly; every
other module reads through the `lava_impact.reports_public` view.
"""
from __future__ import annotations

import datetime
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

_SCHEMA = "lava_impact"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def record_fetch(
    engine: Engine,
    *,
    ein: str | None,
    url_redacted: str,
    kind: str,
    fetch_status: str,
    status_code: int | None = None,
    elapsed_ms: int | None = None,
    notes: str | None = None,
) -> None:
    """Append a row to `lava_impact.fetch_log`. Auto-id; omit `id`."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.fetch_log "
                "(ein, url_redacted, kind, fetch_status, status_code, "
                " fetched_at, elapsed_ms, notes) "
                "VALUES (:ein, :url, :kind, :status, :code, :ts, "
                "        :elapsed, :notes)"
            ),
            {
                "ein": ein,
                "url": url_redacted,
                "kind": kind,
                "status": fetch_status,
                "code": status_code,
                "ts": _now_iso(),
                "elapsed": elapsed_ms,
                "notes": notes,
            },
        )


def upsert_crawled_org(
    engine: Engine,
    *,
    ein: str,
    candidate_count: int,
    fetched_count: int,
    confirmed_report_count: int,
    status: str = "ok",
    max_transient_attempts: int | None = None,
) -> None:
    """Track that this EIN has been processed.

    `confirmed_report_count` uses `GREATEST(existing, new)` so the
    crawler's `0` on re-crawl does not clobber the value backfilled
    by `classify_null.py`.

    `status` values:
      - 'ok'             — successful crawl (default; sync crawler always passes this)
      - 'transient'      — failed transiently; will be retried on next run
      - 'permanent_skip' — explicit permanent failure (SSRF, robots disallow, etc.)

    `attempts` increments by 1 on every upsert. When attempts crosses
    `max_transient_attempts` AND the new status is 'transient', the
    SQL CASE auto-promotes status to 'permanent_skip' so resume stops
    retrying. 'ok' and 'permanent_skip' inputs are persisted as-is.
    """
    if max_transient_attempts is None:
        from . import config as _cfg
        max_transient_attempts = _cfg.MAX_TRANSIENT_ATTEMPTS
    now = _now_iso()
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.crawled_orgs "
                "(ein, first_crawled_at, last_crawled_at, "
                " candidate_count, fetched_count, confirmed_report_count, "
                " status, attempts) "
                "VALUES (:ein, :first, :last, :cand, :fetched, :confirmed, "
                "        :status, 1) "
                "ON CONFLICT (ein) DO UPDATE SET "
                "  last_crawled_at = EXCLUDED.last_crawled_at, "
                "  candidate_count = EXCLUDED.candidate_count, "
                "  fetched_count   = EXCLUDED.fetched_count, "
                "  confirmed_report_count = GREATEST("
                f"    {_SCHEMA}.crawled_orgs.confirmed_report_count, "
                "    EXCLUDED.confirmed_report_count"
                "  ), "
                f"  attempts = {_SCHEMA}.crawled_orgs.attempts + 1, "
                "  status = CASE "
                "    WHEN EXCLUDED.status = 'ok' THEN 'ok' "
                "    WHEN EXCLUDED.status = 'permanent_skip' THEN 'permanent_skip' "
                f"    WHEN {_SCHEMA}.crawled_orgs.attempts + 1 >= :max_attempts "
                "         THEN 'permanent_skip' "
                "    ELSE EXCLUDED.status "
                "  END"
            ),
            {
                "ein": ein,
                "first": now,
                "last": now,
                "cand": candidate_count,
                "fetched": fetched_count,
                "confirmed": confirmed_report_count,
                "status": status,
                "max_attempts": max_transient_attempts,
            },
        )


_UPSERT_REPORT_SQL = text(f"""
INSERT INTO {_SCHEMA}.reports (
  content_sha256, source_url_redacted, referring_page_url_redacted,
  redirect_chain_json, source_org_ein, discovered_via, hosting_platform,
  attribution_confidence, archived_at, content_type,
  file_size_bytes, page_count,
  first_page_text, pdf_creator, pdf_producer, pdf_creation_date,
  pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
  pdf_has_uri_actions, classification, classification_confidence,
  classifier_model, classifier_version, classified_at,
  report_year, report_year_source, extractor_version
) VALUES (
  :sha, :url, :ref, :chain, :ein, :disc, :platform,
  :attr, :archived, :ct,
  :size, :pages,
  :fpt, :creator, :producer, :cdate,
  :js, :launch, :embed,
  :uri, :class, :conf,
  :model, :cver, :cat,
  :year, :ysrc, :ext
)
ON CONFLICT (content_sha256) DO UPDATE SET
  source_url_redacted = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.source_url_redacted
    ELSE {_SCHEMA}.reports.source_url_redacted
  END,
  referring_page_url_redacted = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.referring_page_url_redacted
    ELSE {_SCHEMA}.reports.referring_page_url_redacted
  END,
  redirect_chain_json = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.redirect_chain_json
    ELSE {_SCHEMA}.reports.redirect_chain_json
  END,
  source_org_ein = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.source_org_ein
    ELSE {_SCHEMA}.reports.source_org_ein
  END,
  discovered_via = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.discovered_via
    ELSE {_SCHEMA}.reports.discovered_via
  END,
  hosting_platform = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.hosting_platform
    ELSE {_SCHEMA}.reports.hosting_platform
  END,
  attribution_confidence = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.reports.attribution_confidence)
    THEN EXCLUDED.attribution_confidence
    ELSE {_SCHEMA}.reports.attribution_confidence
  END,
  file_size_bytes = GREATEST({_SCHEMA}.reports.file_size_bytes,
                             EXCLUDED.file_size_bytes),
  page_count          = COALESCE({_SCHEMA}.reports.page_count,
                                 EXCLUDED.page_count),
  first_page_text     = COALESCE({_SCHEMA}.reports.first_page_text,
                                 EXCLUDED.first_page_text),
  pdf_creator         = COALESCE({_SCHEMA}.reports.pdf_creator,
                                 EXCLUDED.pdf_creator),
  pdf_producer        = COALESCE({_SCHEMA}.reports.pdf_producer,
                                 EXCLUDED.pdf_producer),
  pdf_creation_date   = COALESCE({_SCHEMA}.reports.pdf_creation_date,
                                 EXCLUDED.pdf_creation_date),
  pdf_has_javascript  = GREATEST(EXCLUDED.pdf_has_javascript,
                                 {_SCHEMA}.reports.pdf_has_javascript),
  pdf_has_launch      = GREATEST(EXCLUDED.pdf_has_launch,
                                 {_SCHEMA}.reports.pdf_has_launch),
  pdf_has_embedded    = GREATEST(EXCLUDED.pdf_has_embedded,
                                 {_SCHEMA}.reports.pdf_has_embedded),
  pdf_has_uri_actions = GREATEST(EXCLUDED.pdf_has_uri_actions,
                                 {_SCHEMA}.reports.pdf_has_uri_actions),
  classification = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.reports.classification
    WHEN {_SCHEMA}.reports.classification IS NULL
      THEN EXCLUDED.classification
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.reports.classification_confidence, -1)
      THEN EXCLUDED.classification
    ELSE {_SCHEMA}.reports.classification
  END,
  classification_confidence = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.reports.classification_confidence
    WHEN {_SCHEMA}.reports.classification IS NULL
      THEN EXCLUDED.classification_confidence
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.reports.classification_confidence, -1)
      THEN EXCLUDED.classification_confidence
    ELSE {_SCHEMA}.reports.classification_confidence
  END,
  classifier_model = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.reports.classifier_model
    WHEN {_SCHEMA}.reports.classification IS NULL
      THEN EXCLUDED.classifier_model
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.reports.classification_confidence, -1)
      THEN EXCLUDED.classifier_model
    ELSE {_SCHEMA}.reports.classifier_model
  END,
  classifier_version = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.reports.classifier_version
    WHEN {_SCHEMA}.reports.classification IS NULL
      THEN EXCLUDED.classifier_version
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.reports.classification_confidence, -1)
      THEN EXCLUDED.classifier_version
    ELSE {_SCHEMA}.reports.classifier_version
  END,
  classified_at = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.reports.classified_at
    WHEN {_SCHEMA}.reports.classification IS NULL
      THEN EXCLUDED.classified_at
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.reports.classification_confidence, -1)
      THEN EXCLUDED.classified_at
    ELSE {_SCHEMA}.reports.classified_at
  END,
  report_year        = COALESCE({_SCHEMA}.reports.report_year,
                                EXCLUDED.report_year),
  report_year_source = COALESCE({_SCHEMA}.reports.report_year_source,
                                EXCLUDED.report_year_source),
  extractor_version  = GREATEST({_SCHEMA}.reports.extractor_version,
                                EXCLUDED.extractor_version)
""")


def upsert_report(
    engine: Engine,
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
    """Atomic upsert into `lava_impact.reports` with attribution merge.

    The ON CONFLICT UPDATE clause uses `attribution_rank()` to prefer
    stronger attribution tiers on conflicting sha256, and
    confidence-based merge for classification fields. This replaces
    the SQLite-era read-then-write pattern with a single atomic
    statement.
    """
    chain_json = (
        json.dumps(redirect_chain_redacted, ensure_ascii=False)[:2048]
        if redirect_chain_redacted
        else None
    )
    classified_at = _now_iso() if classification is not None else None
    archived_at = _now_iso()

    with engine.begin() as conn:
        conn.execute(
            _UPSERT_REPORT_SQL,
            {
                "sha": content_sha256,
                "url": source_url_redacted,
                "ref": referring_page_url_redacted,
                "chain": chain_json,
                "ein": source_org_ein,
                "disc": discovered_via,
                "platform": hosting_platform,
                "attr": attribution_confidence,
                "archived": archived_at,
                "ct": content_type,
                "size": file_size_bytes,
                "pages": page_count,
                "fpt": first_page_text,
                "creator": pdf_creator,
                "producer": pdf_producer,
                "cdate": pdf_creation_date,
                "js": pdf_has_javascript,
                "launch": pdf_has_launch,
                "embed": pdf_has_embedded,
                "uri": pdf_has_uri_actions,
                "class": classification,
                "conf": classification_confidence,
                "model": classifier_model,
                "cver": classifier_version,
                "cat": classified_at,
                "year": report_year,
                "ysrc": report_year_source,
                "ext": extractor_version,
            },
        )


def record_deletion(
    engine: Engine,
    *,
    content_sha256: str,
    reason: str | None,
    operator: str | None,
    pdf_unlinked: int,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.deletion_log "
                "(content_sha256, deleted_at, reason, operator, pdf_unlinked) "
                "VALUES (:sha, :ts, :reason, :operator, :unlinked)"
            ),
            {
                "sha": content_sha256,
                "ts": _now_iso(),
                "reason": reason,
                "operator": operator,
                "unlinked": pdf_unlinked,
            },
        )


__all__ = [
    "record_fetch",
    "upsert_crawled_org",
    "upsert_report",
    "record_deletion",
]
