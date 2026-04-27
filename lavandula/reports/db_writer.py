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
permitted to reference the `lava_impact.corpus` table directly; every
other module reads through the `lava_impact.corpus_public` view.
"""
from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

_SCHEMA = "lava_impact"


def git_short_sha() -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


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
    notes: str | None = None,
    max_transient_attempts: int | None = None,
    run_id: str | None = None,
    discovery_ms: int | None = None,
    download_ms: int | None = None,
    classify_ms: int | None = None,
    total_ms: int | None = None,
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
                " status, attempts, notes, "
                " run_id, discovery_ms, download_ms, classify_ms, total_ms) "
                "VALUES (:ein, :first, :last, :cand, :fetched, :confirmed, "
                "        :status, 1, :notes, "
                "        :run_id, :discovery_ms, :download_ms, :classify_ms, :total_ms) "
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
                "    WHEN EXCLUDED.status = 'transient' "
                "         AND EXCLUDED.notes = 'wayback_no_coverage' "
                f"         AND {_SCHEMA}.crawled_orgs.status = 'transient' "
                f"         AND {_SCHEMA}.crawled_orgs.notes = 'wayback_no_coverage' "
                "         THEN 'permanent_skip' "
                f"    WHEN {_SCHEMA}.crawled_orgs.attempts + 1 >= :max_attempts "
                "         THEN 'permanent_skip' "
                "    ELSE EXCLUDED.status "
                "  END, "
                "  notes = EXCLUDED.notes, "
                "  run_id = COALESCE(EXCLUDED.run_id, "
                f"    {_SCHEMA}.crawled_orgs.run_id), "
                "  discovery_ms = COALESCE(EXCLUDED.discovery_ms, "
                f"    {_SCHEMA}.crawled_orgs.discovery_ms), "
                "  download_ms = COALESCE(EXCLUDED.download_ms, "
                f"    {_SCHEMA}.crawled_orgs.download_ms), "
                "  classify_ms = COALESCE(EXCLUDED.classify_ms, "
                f"    {_SCHEMA}.crawled_orgs.classify_ms), "
                "  total_ms = COALESCE(EXCLUDED.total_ms, "
                f"    {_SCHEMA}.crawled_orgs.total_ms)"
            ),
            {
                "ein": ein,
                "first": now,
                "last": now,
                "cand": candidate_count,
                "fetched": fetched_count,
                "confirmed": confirmed_report_count,
                "status": status,
                "notes": notes,
                "max_attempts": max_transient_attempts,
                "run_id": run_id,
                "discovery_ms": discovery_ms,
                "download_ms": download_ms,
                "classify_ms": classify_ms,
                "total_ms": total_ms,
            },
        )


_UPSERT_REPORT_SQL = text(f"""
INSERT INTO {_SCHEMA}.corpus (
  content_sha256, source_url_redacted, referring_page_url_redacted,
  redirect_chain_json, source_org_ein, discovered_via, hosting_platform,
  attribution_confidence, archived_at, content_type,
  file_size_bytes, page_count,
  first_page_text, pdf_creator, pdf_producer, pdf_creation_date,
  pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
  pdf_has_uri_actions, classification, classification_confidence,
  classifier_model, classifier_version, classified_at,
  report_year, report_year_source, extractor_version,
  original_source_url_redacted,
  material_type, material_group, event_type,
  reasoning,
  run_id
) VALUES (
  :sha, :url, :ref, :chain, :ein, :disc, :platform,
  :attr, :archived, :ct,
  :size, :pages,
  :fpt, :creator, :producer, :cdate,
  :js, :launch, :embed,
  :uri, :class, :conf,
  :model, :cver, :cat,
  :year, :ysrc, :ext,
  :orig_url,
  :mt, :mg, :et,
  :reasoning,
  :run_id
)
ON CONFLICT (content_sha256) DO UPDATE SET
  source_url_redacted = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.source_url_redacted
    ELSE {_SCHEMA}.corpus.source_url_redacted
  END,
  referring_page_url_redacted = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.referring_page_url_redacted
    ELSE {_SCHEMA}.corpus.referring_page_url_redacted
  END,
  redirect_chain_json = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.redirect_chain_json
    ELSE {_SCHEMA}.corpus.redirect_chain_json
  END,
  source_org_ein = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.source_org_ein
    ELSE {_SCHEMA}.corpus.source_org_ein
  END,
  discovered_via = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.discovered_via
    ELSE {_SCHEMA}.corpus.discovered_via
  END,
  hosting_platform = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.hosting_platform
    ELSE {_SCHEMA}.corpus.hosting_platform
  END,
  attribution_confidence = CASE
    WHEN {_SCHEMA}.attribution_rank(EXCLUDED.attribution_confidence)
       > {_SCHEMA}.attribution_rank({_SCHEMA}.corpus.attribution_confidence)
    THEN EXCLUDED.attribution_confidence
    ELSE {_SCHEMA}.corpus.attribution_confidence
  END,
  file_size_bytes = GREATEST({_SCHEMA}.corpus.file_size_bytes,
                             EXCLUDED.file_size_bytes),
  page_count          = COALESCE({_SCHEMA}.corpus.page_count,
                                 EXCLUDED.page_count),
  first_page_text     = COALESCE({_SCHEMA}.corpus.first_page_text,
                                 EXCLUDED.first_page_text),
  pdf_creator         = COALESCE({_SCHEMA}.corpus.pdf_creator,
                                 EXCLUDED.pdf_creator),
  pdf_producer        = COALESCE({_SCHEMA}.corpus.pdf_producer,
                                 EXCLUDED.pdf_producer),
  pdf_creation_date   = COALESCE({_SCHEMA}.corpus.pdf_creation_date,
                                 EXCLUDED.pdf_creation_date),
  pdf_has_javascript  = GREATEST(EXCLUDED.pdf_has_javascript,
                                 {_SCHEMA}.corpus.pdf_has_javascript),
  pdf_has_launch      = GREATEST(EXCLUDED.pdf_has_launch,
                                 {_SCHEMA}.corpus.pdf_has_launch),
  pdf_has_embedded    = GREATEST(EXCLUDED.pdf_has_embedded,
                                 {_SCHEMA}.corpus.pdf_has_embedded),
  pdf_has_uri_actions = GREATEST(EXCLUDED.pdf_has_uri_actions,
                                 {_SCHEMA}.corpus.pdf_has_uri_actions),
  classification = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.classification
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.classification
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.classification
    ELSE {_SCHEMA}.corpus.classification
  END,
  classification_confidence = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.classification_confidence
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.classification_confidence
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.classification_confidence
    ELSE {_SCHEMA}.corpus.classification_confidence
  END,
  classifier_model = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.classifier_model
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.classifier_model
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.classifier_model
    ELSE {_SCHEMA}.corpus.classifier_model
  END,
  classifier_version = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.classifier_version
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.classifier_version
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.classifier_version
    ELSE {_SCHEMA}.corpus.classifier_version
  END,
  classified_at = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.classified_at
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.classified_at
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.classified_at
    ELSE {_SCHEMA}.corpus.classified_at
  END,
  material_type = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.material_type
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.material_type
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.material_type
    ELSE {_SCHEMA}.corpus.material_type
  END,
  material_group = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.material_group
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.material_group
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.material_group
    ELSE {_SCHEMA}.corpus.material_group
  END,
  event_type = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.event_type
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.event_type
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.event_type
    ELSE {_SCHEMA}.corpus.event_type
  END,
  reasoning = CASE
    WHEN EXCLUDED.classification IS NULL
      THEN {_SCHEMA}.corpus.reasoning
    WHEN {_SCHEMA}.corpus.classification IS NULL
      THEN EXCLUDED.reasoning
    WHEN COALESCE(EXCLUDED.classification_confidence, -1)
       > COALESCE({_SCHEMA}.corpus.classification_confidence, -1)
      THEN EXCLUDED.reasoning
    ELSE {_SCHEMA}.corpus.reasoning
  END,
  report_year        = COALESCE({_SCHEMA}.corpus.report_year,
                                EXCLUDED.report_year),
  report_year_source = COALESCE({_SCHEMA}.corpus.report_year_source,
                                EXCLUDED.report_year_source),
  extractor_version  = GREATEST({_SCHEMA}.corpus.extractor_version,
                                EXCLUDED.extractor_version),
  original_source_url_redacted = COALESCE(
    EXCLUDED.original_source_url_redacted,
    {_SCHEMA}.corpus.original_source_url_redacted
  ),
  run_id = COALESCE(EXCLUDED.run_id, {_SCHEMA}.corpus.run_id)
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
    original_source_url_redacted: str | None = None,
    material_type: str | None = None,
    material_group: str | None = None,
    event_type: str | None = None,
    reasoning: str | None = None,
    run_id: str | None = None,
) -> None:
    """Atomic upsert into `lava_impact.corpus` with attribution merge.

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
                "orig_url": original_source_url_redacted,
                "mt": material_type,
                "mg": material_group,
                "et": event_type,
                "reasoning": reasoning,
                "run_id": run_id,
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


def create_run(
    engine: Engine,
    *,
    run_id: str,
    mode: str,
    code_version: str | None = None,
    config_json: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.runs "
                "(run_id, mode, code_version, started_at, config_json) "
                "VALUES (:run_id, :mode, :code_version, :started_at, :config_json)"
            ),
            {
                "run_id": run_id,
                "mode": mode,
                "code_version": code_version,
                "started_at": _now_iso(),
                "config_json": config_json,
            },
        )


def finish_run(
    engine: Engine,
    *,
    run_id: str,
    stats_json: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {_SCHEMA}.runs "
                "SET finished_at = :finished_at, stats = :stats "
                "WHERE run_id = :run_id"
            ),
            {
                "run_id": run_id,
                "finished_at": _now_iso(),
                "stats": stats_json,
            },
        )


__all__ = [
    "git_short_sha",
    "record_fetch",
    "upsert_crawled_org",
    "upsert_report",
    "record_deletion",
    "create_run",
    "finish_run",
]
