"""Parameterized writes into the spec-0004 schema (AC23 compliance).

Every SQL write goes through `?` parameter binding. This module plus
`catalogue.py` and `schema.py` are the ONLY files permitted to reference
the `reports` table directly (AC23); everything else reads through the
`reports_public` view.

Spec 0013 Phase 3: each public function accepts an optional
`rds_writer` kwarg. When provided, a parallel Postgres-flavored
closure is enqueued on the RDS writer. RDS is best-effort; its
failures never affect the SQLite write or the caller.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any

_RDS_SCHEMA = "lava_impact"

_ATTRIBUTION_RANK = {
    "platform_unverified": 0,
    "platform_verified": 1,
    "own_domain": 2,
}


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _attribution_rank(value: str | None) -> int:
    return _ATTRIBUTION_RANK.get(value or "", -1)


def _prefer_new_source(existing_attr: str | None, new_attr: str) -> bool:
    return _attribution_rank(new_attr) > _attribution_rank(existing_attr)


def _pick_missing(existing: Any, new: Any) -> Any:
    if existing in (None, ""):
        return new
    return existing


def _pick_classification(
    *,
    existing_classification: str | None,
    existing_confidence: float | None,
    existing_model: str,
    existing_version: int,
    existing_classified_at: str | None,
    new_classification: str | None,
    new_confidence: float | None,
    new_model: str,
    new_version: int,
    new_classified_at: str | None,
) -> tuple[str | None, float | None, str, int, str | None]:
    if new_classification is None:
        return (
            existing_classification,
            existing_confidence,
            existing_model,
            existing_version,
            existing_classified_at,
        )
    if existing_classification is None:
        return (
            new_classification,
            new_confidence,
            new_model,
            new_version,
            new_classified_at,
        )
    existing_score = existing_confidence if existing_confidence is not None else -1.0
    new_score = new_confidence if new_confidence is not None else -1.0
    if new_score > existing_score:
        return (
            new_classification,
            new_confidence,
            new_model,
            new_version,
            new_classified_at,
        )
    return (
        existing_classification,
        existing_confidence,
        existing_model,
        existing_version,
        existing_classified_at,
    )


def record_fetch(
    conn: sqlite3.Connection | None,
    *,
    ein: str | None,
    url_redacted: str,
    kind: str,
    fetch_status: str,
    status_code: int | None = None,
    elapsed_ms: int | None = None,
    notes: str | None = None,
    db_writer: Any = None,
    rds_writer: Any = None,
) -> None:
    """Append a row to fetch_log.

    When `db_writer` is provided (TICK-002 parallel mode), the write is
    enqueued on the single-writer thread and `conn` may be `None`.

    When `rds_writer` is also provided (Spec 0013 Phase 3 dual-write),
    a parallel Postgres-flavored closure is enqueued on the RDS
    writer. `fetch_log` is an auto-id table; the `id` column is
    omitted so Postgres assigns a fresh sequence value.
    """
    now_iso = _now_iso()

    def _do(target_conn: sqlite3.Connection) -> None:
        target_conn.execute(
            """
            INSERT INTO fetch_log
              (ein, url_redacted, kind, fetch_status, status_code, fetched_at,
               elapsed_ms, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ein, url_redacted, kind, fetch_status, status_code, now_iso,
             elapsed_ms, notes),
        )

    if db_writer is not None:
        db_writer.put(_do)
    else:
        _do(conn)

    if rds_writer is not None:
        params = (ein, url_redacted, kind, fetch_status, status_code,
                  now_iso, elapsed_ms, notes)

        def _do_rds(pg_conn: Any) -> None:
            with pg_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {_RDS_SCHEMA}.fetch_log
                      (ein, url_redacted, kind, fetch_status, status_code,
                       fetched_at, elapsed_ms, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    params,
                )

        rds_writer.put(_do_rds)


def upsert_crawled_org(
    conn: sqlite3.Connection | None,
    *,
    ein: str,
    candidate_count: int,
    fetched_count: int,
    confirmed_report_count: int,
    db_writer: Any = None,
    rds_writer: Any = None,
) -> None:
    """Track that this EIN has been processed (AC20 resume)."""
    now = _now_iso()

    def _do(target_conn: sqlite3.Connection) -> None:
        # TICK-002: classification is deferred to classify_null.py,
        # which backfills confirmed_report_count. On re-crawl we must
        # NOT overwrite that backfilled value with 0 — preserve the
        # existing count and let the next classify pass update it.
        target_conn.execute(
            """
            INSERT INTO crawled_orgs
              (ein, first_crawled_at, last_crawled_at, candidate_count,
               fetched_count, confirmed_report_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ein) DO UPDATE SET
              last_crawled_at = excluded.last_crawled_at,
              candidate_count = excluded.candidate_count,
              fetched_count = excluded.fetched_count
              -- confirmed_report_count intentionally NOT updated here
            """,
            (ein, now, now, candidate_count, fetched_count,
             confirmed_report_count),
        )

    if db_writer is not None:
        db_writer.put(_do)
    else:
        _do(conn)

    if rds_writer is not None:
        params = (ein, now, now, candidate_count, fetched_count,
                  confirmed_report_count)

        def _do_rds(pg_conn: Any) -> None:
            with pg_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {_RDS_SCHEMA}.crawled_orgs
                      (ein, first_crawled_at, last_crawled_at,
                       candidate_count, fetched_count, confirmed_report_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ein) DO UPDATE SET
                      last_crawled_at = EXCLUDED.last_crawled_at,
                      candidate_count = EXCLUDED.candidate_count,
                      fetched_count = EXCLUDED.fetched_count,
                      -- SQLite-parity: the crawler passes 0 on re-crawl
                      -- and classify_null backfills the real value later.
                      -- GREATEST protects the classify_null-backfilled
                      -- count from being reset to 0 on re-crawl, matching
                      -- SQLite's "intentionally NOT updated here" behavior.
                      confirmed_report_count = GREATEST(
                        {_RDS_SCHEMA}.crawled_orgs.confirmed_report_count,
                        EXCLUDED.confirmed_report_count
                      )
                    """,
                    params,
                )

        rds_writer.put(_do_rds)


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
    db_writer: Any = None,
    rds_writer: Any = None,
) -> None:
    """Insert or improve a fully-shaped row into reports keyed by sha."""
    chain_json = (
        json.dumps(redirect_chain_redacted, ensure_ascii=False)[:2048]
        if redirect_chain_redacted
        else None
    )
    classified_at = _now_iso() if classification is not None else None
    archived_at = _now_iso()

    def _do(target_conn: sqlite3.Connection) -> None:
        _upsert_report_inner(
            target_conn,
            content_sha256=content_sha256,
            source_url_redacted=source_url_redacted,
            referring_page_url_redacted=referring_page_url_redacted,
            chain_json=chain_json,
            source_org_ein=source_org_ein,
            discovered_via=discovered_via,
            hosting_platform=hosting_platform,
            attribution_confidence=attribution_confidence,
            archived_at=archived_at,
            content_type=content_type,
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            first_page_text=first_page_text,
            pdf_creator=pdf_creator,
            pdf_producer=pdf_producer,
            pdf_creation_date=pdf_creation_date,
            pdf_has_javascript=pdf_has_javascript,
            pdf_has_launch=pdf_has_launch,
            pdf_has_embedded=pdf_has_embedded,
            pdf_has_uri_actions=pdf_has_uri_actions,
            classification=classification,
            classification_confidence=classification_confidence,
            classifier_model=classifier_model,
            classifier_version=classifier_version,
            classified_at=classified_at,
            report_year=report_year,
            report_year_source=report_year_source,
            extractor_version=extractor_version,
        )

    if db_writer is not None:
        db_writer.put(_do)
    else:
        _do(conn)

    if rds_writer is not None:
        # Capture the fully-resolved kwargs for the RDS closure. The
        # Postgres side runs a parallel read-merge-write with the same
        # attribution/confidence logic, but against lava_impact.reports.
        rds_kwargs = dict(
            content_sha256=content_sha256,
            source_url_redacted=source_url_redacted,
            referring_page_url_redacted=referring_page_url_redacted,
            chain_json=chain_json,
            source_org_ein=source_org_ein,
            discovered_via=discovered_via,
            hosting_platform=hosting_platform,
            attribution_confidence=attribution_confidence,
            archived_at=archived_at,
            content_type=content_type,
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            first_page_text=first_page_text,
            pdf_creator=pdf_creator,
            pdf_producer=pdf_producer,
            pdf_creation_date=pdf_creation_date,
            pdf_has_javascript=pdf_has_javascript,
            pdf_has_launch=pdf_has_launch,
            pdf_has_embedded=pdf_has_embedded,
            pdf_has_uri_actions=pdf_has_uri_actions,
            classification=classification,
            classification_confidence=classification_confidence,
            classifier_model=classifier_model,
            classifier_version=classifier_version,
            classified_at=classified_at,
            report_year=report_year,
            report_year_source=report_year_source,
            extractor_version=extractor_version,
        )

        def _do_rds(pg_conn: Any) -> None:
            _upsert_report_pg_inner(pg_conn, **rds_kwargs)

        rds_writer.put(_do_rds)
    return


def _upsert_report_inner(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
    source_url_redacted: str,
    referring_page_url_redacted: str | None,
    chain_json: str | None,
    source_org_ein: str,
    discovered_via: str,
    hosting_platform: str | None,
    attribution_confidence: str,
    archived_at: str,
    content_type: str,
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
    classified_at: str | None,
    report_year: int | None,
    report_year_source: str | None,
    extractor_version: int,
) -> None:
    existing = conn.execute(
        "SELECT * FROM reports WHERE content_sha256 = ?",
        (content_sha256,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO reports (
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
        return

    use_new_source = _prefer_new_source(existing["attribution_confidence"], attribution_confidence)
    merged_classification = _pick_classification(
        existing_classification=existing["classification"],
        existing_confidence=existing["classification_confidence"],
        existing_model=existing["classifier_model"],
        existing_version=existing["classifier_version"],
        existing_classified_at=existing["classified_at"],
        new_classification=classification,
        new_confidence=classification_confidence,
        new_model=classifier_model,
        new_version=classifier_version,
        new_classified_at=classified_at,
    )
    conn.execute(
        """
        UPDATE reports
           SET source_url_redacted = ?,
               referring_page_url_redacted = ?,
               redirect_chain_json = ?,
               source_org_ein = ?,
               discovered_via = ?,
               hosting_platform = ?,
               attribution_confidence = ?,
               file_size_bytes = ?,
               page_count = ?,
               first_page_text = ?,
               pdf_creator = ?,
               pdf_producer = ?,
               pdf_creation_date = ?,
               pdf_has_javascript = ?,
               pdf_has_launch = ?,
               pdf_has_embedded = ?,
               pdf_has_uri_actions = ?,
               classification = ?,
               classification_confidence = ?,
               classifier_model = ?,
               classifier_version = ?,
               classified_at = ?,
               report_year = ?,
               report_year_source = ?,
               extractor_version = ?
         WHERE content_sha256 = ?
        """,
        (
            source_url_redacted if use_new_source else existing["source_url_redacted"],
            referring_page_url_redacted if use_new_source else existing["referring_page_url_redacted"],
            chain_json if use_new_source else existing["redirect_chain_json"],
            source_org_ein if use_new_source else existing["source_org_ein"],
            discovered_via if use_new_source else existing["discovered_via"],
            hosting_platform if use_new_source else existing["hosting_platform"],
            attribution_confidence if use_new_source else existing["attribution_confidence"],
            max(existing["file_size_bytes"], file_size_bytes),
            _pick_missing(existing["page_count"], page_count),
            _pick_missing(existing["first_page_text"], first_page_text),
            _pick_missing(existing["pdf_creator"], pdf_creator),
            _pick_missing(existing["pdf_producer"], pdf_producer),
            _pick_missing(existing["pdf_creation_date"], pdf_creation_date),
            1 if existing["pdf_has_javascript"] or pdf_has_javascript else 0,
            1 if existing["pdf_has_launch"] or pdf_has_launch else 0,
            1 if existing["pdf_has_embedded"] or pdf_has_embedded else 0,
            1 if existing["pdf_has_uri_actions"] or pdf_has_uri_actions else 0,
            merged_classification[0],
            merged_classification[1],
            merged_classification[2],
            merged_classification[3],
            merged_classification[4],
            _pick_missing(existing["report_year"], report_year),
            _pick_missing(existing["report_year_source"], report_year_source),
            max(existing["extractor_version"], extractor_version),
            content_sha256,
        ),
    )


def record_deletion(
    conn: sqlite3.Connection | None,
    *,
    content_sha256: str,
    reason: str | None,
    operator: str | None,
    pdf_unlinked: int,
    db_writer: Any = None,
    rds_writer: Any = None,
) -> None:
    now_iso = _now_iso()

    def _do(target_conn: sqlite3.Connection) -> None:
        target_conn.execute(
            """
            INSERT INTO deletion_log
              (content_sha256, deleted_at, reason, operator, pdf_unlinked)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content_sha256, now_iso, reason, operator, pdf_unlinked),
        )

    if db_writer is not None:
        db_writer.put(_do)
    else:
        _do(conn)

    if rds_writer is not None:
        params = (content_sha256, now_iso, reason, operator, pdf_unlinked)

        def _do_rds(pg_conn: Any) -> None:
            with pg_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {_RDS_SCHEMA}.deletion_log
                      (content_sha256, deleted_at, reason, operator, pdf_unlinked)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    params,
                )

        rds_writer.put(_do_rds)


def _is_unique_violation(exc: BaseException) -> bool:
    """Identify psycopg2 UniqueViolation without a hard import dependency.

    Tests and some environments may not have psycopg2 on the path, so
    we duck-type the SQLSTATE ('23505' = unique_violation per Postgres).
    """
    pgcode = getattr(exc, "pgcode", None)
    if pgcode == "23505":
        return True
    cls_name = exc.__class__.__name__
    if cls_name == "UniqueViolation":
        return True
    return False


_UPSERT_REPORT_MAX_ATTEMPTS = 2


def _upsert_report_pg_inner(
    pg_conn: Any,
    *,
    content_sha256: str,
    source_url_redacted: str,
    referring_page_url_redacted: str | None,
    chain_json: str | None,
    source_org_ein: str,
    discovered_via: str,
    hosting_platform: str | None,
    attribution_confidence: str,
    archived_at: str,
    content_type: str,
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
    classified_at: str | None,
    report_year: int | None,
    report_year_source: str | None,
    extractor_version: int,
) -> None:
    """Postgres-flavored parallel of `_upsert_report_inner` for RDS.

    Mirrors the same read-merge-write semantics (attribution rank,
    classification confidence, monotonic extractor_version, etc.) but
    targets `lava_impact.reports` with %s placeholders. Called only
    from the `rds_writer` closure path; SQLite path is unchanged.

    Race handling: the crawler and classify_null each run independent
    `RDSDBWriter`s, so two processes can SELECT (miss) → INSERT the
    same `content_sha256` and lose the race on the Postgres unique
    key. We catch `UniqueViolation` (SQLSTATE 23505), rollback to a
    savepoint, and retry the read-merge path which now sees the
    other writer's row and lands in the UPDATE branch. Bounded to
    `_UPSERT_REPORT_MAX_ATTEMPTS` to avoid pathological loops.
    """
    kwargs = dict(
        content_sha256=content_sha256,
        source_url_redacted=source_url_redacted,
        referring_page_url_redacted=referring_page_url_redacted,
        chain_json=chain_json,
        source_org_ein=source_org_ein,
        discovered_via=discovered_via,
        hosting_platform=hosting_platform,
        attribution_confidence=attribution_confidence,
        archived_at=archived_at,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
        page_count=page_count,
        first_page_text=first_page_text,
        pdf_creator=pdf_creator,
        pdf_producer=pdf_producer,
        pdf_creation_date=pdf_creation_date,
        pdf_has_javascript=pdf_has_javascript,
        pdf_has_launch=pdf_has_launch,
        pdf_has_embedded=pdf_has_embedded,
        pdf_has_uri_actions=pdf_has_uri_actions,
        classification=classification,
        classification_confidence=classification_confidence,
        classifier_model=classifier_model,
        classifier_version=classifier_version,
        classified_at=classified_at,
        report_year=report_year,
        report_year_source=report_year_source,
        extractor_version=extractor_version,
    )
    sp_name = "upsert_report_rds"
    last_exc: BaseException | None = None
    for _attempt in range(_UPSERT_REPORT_MAX_ATTEMPTS):
        with pg_conn.cursor() as sp_cur:
            sp_cur.execute(f"SAVEPOINT {sp_name}")
        try:
            _upsert_report_pg_body(pg_conn, **kwargs)
            with pg_conn.cursor() as sp_cur:
                sp_cur.execute(f"RELEASE SAVEPOINT {sp_name}")
            return
        except Exception as exc:  # noqa: BLE001
            with pg_conn.cursor() as sp_cur:
                sp_cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
            if _is_unique_violation(exc):
                # Another writer inserted this sha between our SELECT
                # and INSERT. Retry — the second pass will see the
                # row and take the UPDATE branch.
                last_exc = exc
                continue
            raise
    # Exhausted attempts — surface the last UniqueViolation. The
    # best-effort RDSDBWriter will log WARN and drop the op; SQLite
    # remains untouched.
    if last_exc is not None:
        raise last_exc


def _upsert_report_pg_body(
    pg_conn: Any,
    *,
    content_sha256: str,
    source_url_redacted: str,
    referring_page_url_redacted: str | None,
    chain_json: str | None,
    source_org_ein: str,
    discovered_via: str,
    hosting_platform: str | None,
    attribution_confidence: str,
    archived_at: str,
    content_type: str,
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
    classified_at: str | None,
    report_year: int | None,
    report_year_source: str | None,
    extractor_version: int,
) -> None:
    """One read-merge-write attempt against RDS. Called by the retry
    loop in `_upsert_report_pg_inner`."""
    with pg_conn.cursor() as cur:
        cur.execute(
            f"SELECT source_url_redacted, referring_page_url_redacted, "
            f"       redirect_chain_json, source_org_ein, discovered_via, "
            f"       hosting_platform, attribution_confidence, "
            f"       file_size_bytes, page_count, first_page_text, "
            f"       pdf_creator, pdf_producer, pdf_creation_date, "
            f"       pdf_has_javascript, pdf_has_launch, pdf_has_embedded, "
            f"       pdf_has_uri_actions, classification, "
            f"       classification_confidence, classifier_model, "
            f"       classifier_version, classified_at, report_year, "
            f"       report_year_source, extractor_version "
            f"FROM {_RDS_SCHEMA}.reports WHERE content_sha256 = %s",
            (content_sha256,),
        )
        row = cur.fetchone()

        if row is None:
            cur.execute(
                f"""
                INSERT INTO {_RDS_SCHEMA}.reports (
                  content_sha256, source_url_redacted,
                  referring_page_url_redacted, redirect_chain_json,
                  source_org_ein, discovered_via, hosting_platform,
                  attribution_confidence, archived_at, content_type,
                  file_size_bytes, page_count, first_page_text,
                  pdf_creator, pdf_producer, pdf_creation_date,
                  pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
                  pdf_has_uri_actions, classification,
                  classification_confidence, classifier_model,
                  classifier_version, classified_at, report_year,
                  report_year_source, extractor_version
                ) VALUES (
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    content_sha256, source_url_redacted,
                    referring_page_url_redacted, chain_json,
                    source_org_ein, discovered_via, hosting_platform,
                    attribution_confidence, archived_at, content_type,
                    file_size_bytes, page_count, first_page_text,
                    pdf_creator, pdf_producer, pdf_creation_date,
                    pdf_has_javascript, pdf_has_launch, pdf_has_embedded,
                    pdf_has_uri_actions, classification,
                    classification_confidence, classifier_model,
                    classifier_version, classified_at, report_year,
                    report_year_source, extractor_version,
                ),
            )
            return

        (existing_source_url, existing_referring, existing_chain_json,
         existing_ein, existing_discovered, existing_platform,
         existing_attr, existing_size, existing_page_count,
         existing_first_page, existing_pdf_creator,
         existing_pdf_producer, existing_pdf_creation, existing_has_js,
         existing_has_launch, existing_has_embedded, existing_has_uri,
         existing_classification, existing_confidence,
         existing_model, existing_version, existing_classified_at,
         existing_report_year, existing_report_year_source,
         existing_extractor_version) = row

        use_new_source = _prefer_new_source(existing_attr, attribution_confidence)
        merged = _pick_classification(
            existing_classification=existing_classification,
            existing_confidence=existing_confidence,
            existing_model=existing_model,
            existing_version=existing_version,
            existing_classified_at=existing_classified_at,
            new_classification=classification,
            new_confidence=classification_confidence,
            new_model=classifier_model,
            new_version=classifier_version,
            new_classified_at=classified_at,
        )
        cur.execute(
            f"""
            UPDATE {_RDS_SCHEMA}.reports
               SET source_url_redacted = %s,
                   referring_page_url_redacted = %s,
                   redirect_chain_json = %s,
                   source_org_ein = %s,
                   discovered_via = %s,
                   hosting_platform = %s,
                   attribution_confidence = %s,
                   file_size_bytes = %s,
                   page_count = %s,
                   first_page_text = %s,
                   pdf_creator = %s,
                   pdf_producer = %s,
                   pdf_creation_date = %s,
                   pdf_has_javascript = %s,
                   pdf_has_launch = %s,
                   pdf_has_embedded = %s,
                   pdf_has_uri_actions = %s,
                   classification = %s,
                   classification_confidence = %s,
                   classifier_model = %s,
                   classifier_version = %s,
                   classified_at = %s,
                   report_year = %s,
                   report_year_source = %s,
                   extractor_version = %s
             WHERE content_sha256 = %s
            """,
            (
                source_url_redacted if use_new_source else existing_source_url,
                referring_page_url_redacted if use_new_source else existing_referring,
                chain_json if use_new_source else existing_chain_json,
                source_org_ein if use_new_source else existing_ein,
                discovered_via if use_new_source else existing_discovered,
                hosting_platform if use_new_source else existing_platform,
                attribution_confidence if use_new_source else existing_attr,
                max(existing_size or 0, file_size_bytes),
                _pick_missing(existing_page_count, page_count),
                _pick_missing(existing_first_page, first_page_text),
                _pick_missing(existing_pdf_creator, pdf_creator),
                _pick_missing(existing_pdf_producer, pdf_producer),
                _pick_missing(existing_pdf_creation, pdf_creation_date),
                1 if existing_has_js or pdf_has_javascript else 0,
                1 if existing_has_launch or pdf_has_launch else 0,
                1 if existing_has_embedded or pdf_has_embedded else 0,
                1 if existing_has_uri or pdf_has_uri_actions else 0,
                merged[0], merged[1], merged[2], merged[3], merged[4],
                _pick_missing(existing_report_year, report_year),
                _pick_missing(existing_report_year_source, report_year_source),
                max(existing_extractor_version or 0, extractor_version),
                content_sha256,
            ),
        )


__all__ = [
    "record_fetch",
    "upsert_crawled_org",
    "upsert_report",
    "record_deletion",
    "_upsert_report_pg_inner",
]
