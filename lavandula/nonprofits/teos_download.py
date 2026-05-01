"""Zip downloader and batch processor for IRS 990 XML filings (Spec 0026).

Downloads TEOS zip archives, extracts individual XML members,
parses them, and upserts people rows into the database.
"""
from __future__ import annotations

import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .irs990_parser import ParseResult, parse_990_xml

log = logging.getLogger(__name__)

TEOS_ZIP_URL = (
    "https://apps.irs.gov/pub/epostcard/990/xml/{filing_year}/{xml_batch_id}.zip"
)

_MAX_MEMBER_SIZE = 50 * 1024 * 1024  # 50 MB
_MEMBER_NAME_RE = re.compile(r"^([\w]+/)?\d+_public\.xml$")
_MAX_RETRIES = 3
_RETRY_DELAYS = [2, 4, 8]
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ERROR_MSG_LEN = 500


@dataclass
class ProcessStats:
    filings_processed: int = 0
    filings_parsed: int = 0
    filings_skipped: int = 0
    filings_error: int = 0
    people_upserted: int = 0
    zips_downloaded: int = 0
    zips_cached: int = 0
    schedule_j_matched: int = 0


ShutdownFlag = type("ShutdownFlag", (), {"is_set": lambda self: False})
try:
    from .pipeline_resolver import ShutdownFlag
except ImportError:
    pass


def _sanitize_error(msg: str) -> str:
    return msg[:_MAX_ERROR_MSG_LEN]


def _download_zip(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".zip.tmp")
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = requests.get(url, stream=True, timeout=120)
                if resp.status_code == 404:
                    raise FileNotFoundError(f"404 for {url}")
                if resp.status_code == 403:
                    raise PermissionError(f"403 for {url}")
                if resp.status_code in _RETRYABLE_STATUS:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_DELAYS[attempt]
                        log.warning(
                            "HTTP %d for %s, retrying in %ds",
                            resp.status_code, url, delay,
                        )
                        time.sleep(delay)
                        continue
                    resp.raise_for_status()

                resp.raise_for_status()

                expected_len = resp.headers.get("Content-Length")
                written = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                        written += len(chunk)

                if expected_len is not None and written != int(expected_len):
                    tmp.unlink(missing_ok=True)
                    if attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_DELAYS[attempt]
                        log.warning(
                            "Truncated download for %s (%d/%s bytes), "
                            "retrying in %ds",
                            url, written, expected_len, delay,
                        )
                        time.sleep(delay)
                        continue
                    raise IOError(
                        f"Truncated download: {written}/{expected_len} bytes"
                    )

                os.rename(str(tmp), str(dest))
                return

            except requests.ConnectionError:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    log.warning(
                        "Connection error for %s, retrying in %ds",
                        url, delay,
                    )
                    time.sleep(delay)
                    continue
                raise
    finally:
        tmp.unlink(missing_ok=True)


def _upsert_people(
    conn, result: ParseResult, object_id: str, run_id: str,
) -> int:
    if not result.people:
        return 0

    upsert_sql = text("""
        INSERT INTO lava_corpus.people
            (ein, tax_period, object_id, person_name, title, person_type,
             avg_hours_per_week, reportable_comp, related_org_comp, other_comp,
             base_comp, bonus, other_reportable, deferred_comp,
             nontaxable_benefits, total_comp_sch_j,
             services_desc, is_officer, is_director, is_key_employee,
             is_highest_comp, is_former, run_id)
        VALUES
            (:ein, :tax_period, :object_id, :person_name, :title, :person_type,
             :avg_hours_per_week, :reportable_comp, :related_org_comp, :other_comp,
             :base_comp, :bonus, :other_reportable, :deferred_comp,
             :nontaxable_benefits, :total_comp_sch_j,
             :services_desc, :is_officer, :is_director, :is_key_employee,
             :is_highest_comp, :is_former, :run_id)
        ON CONFLICT (ein, object_id, person_name, person_type) DO UPDATE SET
            title = EXCLUDED.title,
            avg_hours_per_week = EXCLUDED.avg_hours_per_week,
            reportable_comp = EXCLUDED.reportable_comp,
            related_org_comp = EXCLUDED.related_org_comp,
            other_comp = EXCLUDED.other_comp,
            base_comp = EXCLUDED.base_comp,
            bonus = EXCLUDED.bonus,
            other_reportable = EXCLUDED.other_reportable,
            deferred_comp = EXCLUDED.deferred_comp,
            nontaxable_benefits = EXCLUDED.nontaxable_benefits,
            total_comp_sch_j = EXCLUDED.total_comp_sch_j,
            services_desc = EXCLUDED.services_desc,
            is_officer = EXCLUDED.is_officer,
            is_director = EXCLUDED.is_director,
            is_key_employee = EXCLUDED.is_key_employee,
            is_highest_comp = EXCLUDED.is_highest_comp,
            is_former = EXCLUDED.is_former,
            extracted_at = NOW(),
            run_id = EXCLUDED.run_id
    """)

    schedule_j_sql = text("""
        UPDATE lava_corpus.people
        SET base_comp = :base_comp,
            bonus = :bonus,
            other_reportable = :other_reportable,
            deferred_comp = :deferred_comp,
            nontaxable_benefits = :nontaxable_benefits,
            total_comp_sch_j = :total_comp_sch_j
        WHERE object_id = :object_id AND person_name = :person_name
    """)

    count = 0
    meta = result.metadata
    for p in result.people:
        conn.execute(upsert_sql, {
            "ein": meta.ein,
            "tax_period": meta.tax_period,
            "object_id": object_id,
            "person_name": p.person_name,
            "title": p.title,
            "person_type": p.person_type,
            "avg_hours_per_week": float(p.avg_hours_per_week) if p.avg_hours_per_week is not None else None,
            "reportable_comp": p.reportable_comp,
            "related_org_comp": p.related_org_comp,
            "other_comp": p.other_comp,
            "base_comp": p.base_comp,
            "bonus": p.bonus,
            "other_reportable": p.other_reportable,
            "deferred_comp": p.deferred_comp,
            "nontaxable_benefits": p.nontaxable_benefits,
            "total_comp_sch_j": p.total_comp_sch_j,
            "services_desc": p.services_desc,
            "is_officer": p.is_officer,
            "is_director": p.is_director,
            "is_key_employee": p.is_key_employee,
            "is_highest_comp": p.is_highest_comp,
            "is_former": p.is_former,
            "run_id": run_id,
        })
        count += 1

        if p.base_comp is not None:
            conn.execute(schedule_j_sql, {
                "object_id": object_id,
                "person_name": p.person_name,
                "base_comp": p.base_comp,
                "bonus": p.bonus,
                "other_reportable": p.other_reportable,
                "deferred_comp": p.deferred_comp,
                "nontaxable_benefits": p.nontaxable_benefits,
                "total_comp_sch_j": p.total_comp_sch_j,
            })

    return count


def process_filings(
    *,
    engine: Engine,
    cache_dir: Path,
    skip_download: bool = False,
    reparse: bool = False,
    run_id: str,
    ein_set: set[str] | None = None,
    filing_years: list[int] | None = None,
    shutdown: ShutdownFlag | None = None,
) -> ProcessStats:
    """Group filings by batch, download zips, parse XMLs, upsert people."""
    stats = ProcessStats()
    if shutdown is None:
        shutdown = ShutdownFlag()

    if reparse:
        _reset_for_reparse(engine, ein_set=ein_set, filing_years=filing_years)

    with engine.connect() as conn:
        conditions = ["status IN ('indexed', 'downloaded')"]
        params: dict = {}
        if ein_set:
            conditions.append("ein = ANY(:eins)")
            params["eins"] = list(ein_set)
        if filing_years:
            conditions.append("filing_year = ANY(:years)")
            params["years"] = filing_years
        where = " AND ".join(conditions)
        rows = conn.execute(text(
            f"SELECT object_id, ein, tax_period, xml_batch_id, filing_year "
            f"FROM lava_corpus.filing_index "
            f"WHERE {where} "
            f"ORDER BY filing_year, xml_batch_id, object_id"
        ), params).fetchall()

    batches: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        key = (r[4], r[3])  # (filing_year, xml_batch_id)
        batches.setdefault(key, []).append({
            "object_id": r[0],
            "ein": r[1],
            "tax_period": r[2],
        })

    log.info(
        "Processing %d filings in %d batches", len(rows), len(batches),
    )

    last_download_time = 0.0

    for (filing_year, xml_batch_id), filings in batches.items():
        if shutdown.is_set():
            break

        zip_path = cache_dir / f"{xml_batch_id}.zip"

        if not zip_path.exists():
            if skip_download:
                log.warning(
                    "Cache miss for %s (--skip-download), leaving %d filings "
                    "as indexed",
                    xml_batch_id, len(filings),
                )
                continue

            url = TEOS_ZIP_URL.format(
                filing_year=filing_year, xml_batch_id=xml_batch_id,
            )

            elapsed = time.monotonic() - last_download_time
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            try:
                _download_zip(url, zip_path)
                stats.zips_downloaded += 1
                last_download_time = time.monotonic()
            except FileNotFoundError:
                log.error("Zip not found: %s", url)
                _mark_batch_error(
                    engine, filings,
                    f"Zip not found: {xml_batch_id}.zip", run_id,
                )
                stats.filings_error += len(filings)
                continue
            except (PermissionError, requests.RequestException, IOError) as exc:
                log.error("Download failed for %s: %s", url, exc)
                _mark_batch_error(
                    engine, filings,
                    _sanitize_error(f"Download failed: {type(exc).__name__}"),
                    run_id,
                )
                stats.filings_error += len(filings)
                continue
        else:
            stats.zips_cached += 1

        _mark_batch_downloaded(engine, filings, run_id)

        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile:
            log.error("Corrupt zip file: %s", zip_path)
            _mark_batch_error(
                engine, filings,
                f"Corrupt zip file: {xml_batch_id}.zip", run_id,
            )
            stats.filings_error += len(filings)
            continue

        with zf:
            for filing in filings:
                if shutdown.is_set():
                    break
                _process_single_filing(
                    engine=engine,
                    zf=zf,
                    filing=filing,
                    xml_batch_id=xml_batch_id,
                    run_id=run_id,
                    stats=stats,
                )

    return stats


def _process_single_filing(
    *,
    engine: Engine,
    zf: zipfile.ZipFile,
    filing: dict,
    xml_batch_id: str,
    run_id: str,
    stats: ProcessStats,
) -> None:
    object_id = filing["object_id"]
    # IRS zips use nested paths (2024: {batch}/{oid}_public.xml) or flat (2025: {oid}_public.xml)
    nested_name = f"{xml_batch_id}/{object_id}_public.xml"
    flat_name = f"{object_id}_public.xml"

    try:
        info = zf.getinfo(nested_name)
        member_name = nested_name
    except KeyError:
        try:
            info = zf.getinfo(flat_name)
            member_name = flat_name
        except KeyError:
            log.error("Member %s not found in zip (tried nested and flat)", object_id)
            _mark_filing_error(
                engine, object_id,
                f"Missing member {object_id}_public.xml in zip", run_id,
            )
            stats.filings_error += 1
            return

    if not _MEMBER_NAME_RE.match(info.filename):
        _mark_filing_error(
            engine, object_id,
            f"Suspicious member name: {info.filename[:100]}", run_id,
        )
        stats.filings_error += 1
        return

    if ".." in info.filename:
        _mark_filing_error(
            engine, object_id,
            f"Path traversal in member name: {info.filename[:100]}", run_id,
        )
        stats.filings_error += 1
        return

    if info.file_size > _MAX_MEMBER_SIZE:
        _mark_filing_error(
            engine, object_id,
            f"Member too large: {info.file_size} bytes (max {_MAX_MEMBER_SIZE})",
            run_id,
        )
        stats.filings_error += 1
        return

    try:
        xml_bytes = zf.read(member_name)
    except Exception as exc:
        _mark_filing_error(
            engine, object_id,
            _sanitize_error(f"Zip read error: {type(exc).__name__}"), run_id,
        )
        stats.filings_error += 1
        return

    try:
        result = parse_990_xml(xml_bytes)
    except Exception as exc:
        _mark_filing_error(
            engine, object_id,
            _sanitize_error(f"XML parse error: {type(exc).__name__}"), run_id,
        )
        stats.filings_error += 1
        return

    for w in result.warnings:
        if w.startswith("ERROR:"):
            log.error("[%s] %s", object_id, w)
        else:
            log.warning("[%s] %s", object_id, w)

    if not result.people:
        _mark_filing_skipped(engine, object_id, run_id)
        stats.filings_skipped += 1
        stats.filings_processed += 1
        return

    with engine.begin() as conn:
        count = _upsert_people(conn, result, object_id, run_id)
        stats.people_upserted += count

        sch_j_matched = sum(
            1 for p in result.people if p.base_comp is not None
        )
        stats.schedule_j_matched += sch_j_matched

        conn.execute(text("""
            UPDATE lava_corpus.filing_index
            SET status = 'parsed',
                parsed_at = NOW(),
                return_ts = :return_ts,
                is_amended = :is_amended,
                run_id = :run_id,
                error_message = NULL
            WHERE object_id = :object_id
        """), {
            "object_id": object_id,
            "return_ts": result.metadata.return_ts,
            "is_amended": result.metadata.is_amended,
            "run_id": run_id,
        })

    stats.filings_parsed += 1
    stats.filings_processed += 1


def _reset_for_reparse(
    engine: Engine,
    *,
    ein_set: set[str] | None = None,
    filing_years: list[int] | None = None,
) -> None:
    conditions = ["status IN ('parsed', 'skipped', 'error')"]
    params: dict = {}

    if ein_set:
        conditions.append("ein = ANY(:eins)")
        params["eins"] = list(ein_set)
    if filing_years:
        conditions.append("filing_year = ANY(:years)")
        params["years"] = filing_years

    where = " AND ".join(conditions)
    sql = text(f"""
        UPDATE lava_corpus.filing_index
        SET status = 'downloaded',
            error_message = NULL,
            parsed_at = NULL
        WHERE {where}
    """)

    with engine.begin() as conn:
        result = conn.execute(sql, params)
        log.info("Reparse: reset %d filings to 'downloaded'", result.rowcount)


def _mark_batch_downloaded(
    engine: Engine, filings: list[dict], run_id: str,
) -> None:
    with engine.begin() as conn:
        for f in filings:
            conn.execute(text("""
                UPDATE lava_corpus.filing_index
                SET status = CASE WHEN status = 'indexed' THEN 'downloaded' ELSE status END,
                    run_id = :run_id
                WHERE object_id = :object_id
            """), {"object_id": f["object_id"], "run_id": run_id})


def _mark_batch_error(
    engine: Engine, filings: list[dict], msg: str, run_id: str,
) -> None:
    with engine.begin() as conn:
        for f in filings:
            conn.execute(text("""
                UPDATE lava_corpus.filing_index
                SET status = 'error',
                    error_message = :msg,
                    run_id = :run_id
                WHERE object_id = :object_id
            """), {"object_id": f["object_id"], "msg": _sanitize_error(msg), "run_id": run_id})


def _mark_filing_error(
    engine: Engine, object_id: str, msg: str, run_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE lava_corpus.filing_index
            SET status = 'error',
                error_message = :msg,
                run_id = :run_id
            WHERE object_id = :object_id
        """), {"object_id": object_id, "msg": _sanitize_error(msg), "run_id": run_id})


def _mark_filing_skipped(
    engine: Engine, object_id: str, run_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE lava_corpus.filing_index
            SET status = 'skipped',
                parsed_at = NOW(),
                run_id = :run_id,
                error_message = NULL
            WHERE object_id = :object_id
        """), {"object_id": object_id, "run_id": run_id})
