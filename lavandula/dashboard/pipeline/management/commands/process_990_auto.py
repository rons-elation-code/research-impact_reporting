"""Auto-process 990 filings for tracked orgs via S3 (Spec 0030).

Downloads batch zips (cached in S3), extracts per-org XMLs, parses them,
and upserts people data. Reconciliation-based: processes any filing_index
rows for nonprofits_seed EINs that are still at status='indexed'.

Usage:
    python3 manage.py process_990_auto                # incremental (last 7 days)
    python3 manage.py process_990_auto --backfill     # all unprocessed
    python3 manage.py process_990_auto --ein 131624241
    python3 manage.py process_990_auto --reparse      # retry error filings
"""
from __future__ import annotations

import concurrent.futures
import enum
import io
import ipaddress
import logging
import re
import socket
import time
import unicodedata
import zipfile
from urllib.parse import urlparse

import defusedxml.ElementTree as ET
import requests
from django.core.management.base import BaseCommand
from sqlalchemy import text

from lavandula.common.db import make_app_engine
from lavandula.nonprofits.irs990_parser import parse_990_xml
from lavandula.nonprofits.s3_990 import S3990Archive

log = logging.getLogger(__name__)

TEOS_ZIP_URL = (
    "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch_id}.zip"
)

_LOCK_KEY = "990-family"
_LOCK_TIMEOUT = "1h"

_MAX_MEMBER_SIZE = 50 * 1024 * 1024  # 50 MB
_MAX_ZIP_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB
_MAX_BATCH_EXTRACTED = 10 * 1024 * 1024 * 1024  # 10 GB per batch
_MAX_MEMBERS_PER_ZIP = 200_000
_BOMB_RATIO = 100
_XML_PARSE_TIMEOUT = 30

_MEMBER_NAME_RE = re.compile(r"^([\w]+/)?\d+_public\.xml$", re.ASCII)
_EIN_RE = re.compile(r"^\d{9}$", re.ASCII)
_OBJECT_ID_RE = re.compile(r"^\d+$", re.ASCII)
_BATCH_ID_RE = re.compile(r"^\d{4}_TEOS_XML_(0[1-9]|1[0-2])[A-D]$", re.ASCII)

_RETRY_DELAYS = [2, 4, 8]
_MAX_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DOWNLOAD_DELAY = 5


class ErrorCode(enum.Enum):
    ZIP_MEMBER_MISSING = "ZIP_MEMBER_MISSING"
    ZIP_BOMB_DETECTED = "ZIP_BOMB_DETECTED"
    ZIP_CORRUPT = "ZIP_CORRUPT"
    ZIP_DOWNLOAD_FAILED = "ZIP_DOWNLOAD_FAILED"
    ZIP_MEMBER_INVALID_NAME = "ZIP_MEMBER_INVALID_NAME"
    ZIP_MEMBER_TOO_LARGE = "ZIP_MEMBER_TOO_LARGE"
    XML_PARSE_FAILED = "XML_PARSE_FAILED"
    XML_PARSE_TIMEOUT = "XML_PARSE_TIMEOUT"
    S3_UPLOAD_FAILED = "S3_UPLOAD_FAILED"
    S3_OBJECT_MISSING = "S3_OBJECT_MISSING"
    BATCH_EXTRACT_LIMIT = "BATCH_EXTRACT_LIMIT"


def _sanitize_detail(detail: str) -> str:
    """Strip potentially sensitive info from error details."""
    detail = re.sub(r"https?://[^\s]+", "[URL]", detail)
    detail = re.sub(r"/[\w/.-]+", "[PATH]", detail)
    return detail[:200]


def _resolve_and_check_host(hostname: str) -> None:
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve: {hostname}")
    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"Private IP detected for {hostname}")


def _download_zip_to_s3(
    year: int, batch_id: str, archive: S3990Archive
) -> str | None:
    """Download IRS zip and upload to S3. Returns checksum or None on failure."""
    url = TEOS_ZIP_URL.format(year=year, batch_id=batch_id)
    parsed = urlparse(url)

    if parsed.hostname != "apps.irs.gov" or parsed.scheme != "https":
        raise ValueError(f"Invalid IRS URL: {url}")
    _resolve_and_check_host(parsed.hostname)

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(
                url, stream=True, timeout=300, allow_redirects=False
            )

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                rp = urlparse(location)
                if rp.hostname != "apps.irs.gov" or rp.scheme != "https":
                    raise ValueError(f"Redirect to non-IRS host: {location}")
                _resolve_and_check_host(rp.hostname)
                resp = requests.get(
                    location, stream=True, timeout=300, allow_redirects=False
                )

            if resp.status_code == 404:
                return None

            if resp.status_code in _RETRYABLE_STATUS:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                resp.raise_for_status()

            resp.raise_for_status()

            buf = io.BytesIO()
            bytes_read = 0
            for chunk in resp.iter_content(chunk_size=65536):
                bytes_read += len(chunk)
                if bytes_read > _MAX_ZIP_SIZE:
                    raise ValueError(
                        f"Zip exceeds {_MAX_ZIP_SIZE} byte limit"
                    )
                buf.write(chunk)

            buf.seek(0)
            checksum = archive.upload_zip(year, batch_id, buf)
            return checksum

        except requests.ConnectionError:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise

    return None


def _parse_xml_with_timeout(xml_bytes: bytes):
    """Parse 990 XML with a process-level timeout."""
    with concurrent.futures.ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(parse_990_xml, xml_bytes)
        try:
            return future.result(timeout=_XML_PARSE_TIMEOUT)
        except concurrent.futures.TimeoutError:
            return None


def _record_filing_error(
    engine, object_id: str, code: ErrorCode, detail: str = ""
):
    safe_detail = _sanitize_detail(detail)
    msg = f"{code.value}: {safe_detail}" if safe_detail else code.value
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE lava_corpus.filing_index
            SET status = 'error', error_message = :msg
            WHERE object_id = :oid
        """), {"msg": msg[:500], "oid": object_id})


class Command(BaseCommand):
    help = "Auto-process 990 filings for tracked orgs (download, extract, parse)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="Process all unprocessed filings (no time filter)",
        )
        parser.add_argument(
            "--ein",
            type=str,
            default=None,
            help="Process only filings for this EIN",
        )
        parser.add_argument(
            "--reparse",
            action="store_true",
            help="Also re-process filings in error state",
        )

    def handle(self, *args, **options):
        engine = make_app_engine()
        archive = S3990Archive()

        with engine.connect() as conn:
            conn.execute(
                text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
            )
            try:
                conn.execute(
                    text("SELECT pg_advisory_lock(hashtext(:key))"),
                    {"key": _LOCK_KEY},
                )
            except Exception as e:
                self.stderr.write(
                    f"Could not acquire advisory lock (another job running?): {e}"
                )
                return
            conn.commit()

        try:
            self._run(engine, archive, options)
        finally:
            with engine.connect() as conn:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"),
                    {"key": _LOCK_KEY},
                )
                conn.commit()

    def _run(self, engine, archive: S3990Archive, options: dict):
        ein_filter = options.get("ein")
        if ein_filter and not _EIN_RE.match(ein_filter):
            self.stderr.write(f"Invalid EIN: {ein_filter}")
            return

        if options["reparse"]:
            self._reset_errors(engine, ein_filter, options["backfill"])

        # Pass 1: Download indexed filings
        download_filings = self._query_filings(
            engine, "indexed", ein_filter, options["backfill"]
        )
        # Pass 2: Parse already-downloaded filings
        parse_filings = self._query_filings(
            engine, "downloaded", ein_filter, backfill=True
        )

        stats = {
            "downloaded": 0, "parsed": 0, "errors": 0, "skipped": 0
        }

        if download_filings:
            self._process_download_pass(
                engine, archive, download_filings, stats
            )

        if parse_filings:
            self._process_parse_pass(engine, archive, parse_filings, stats)

        self.stdout.write(
            f"Done: downloaded={stats['downloaded']} parsed={stats['parsed']} "
            f"errors={stats['errors']} skipped={stats['skipped']}"
        )

    def _query_filings(
        self, engine, status: str, ein_filter: str | None, backfill: bool
    ) -> list[dict]:
        conditions = [
            "fi.status = :status",
            "fi.xml_batch_id IS NOT NULL",
            "fi.ein IN (SELECT ein FROM lava_corpus.nonprofits_seed)",
        ]
        params = {"status": status}

        if ein_filter:
            conditions.append("fi.ein = :ein")
            params["ein"] = ein_filter

        if not backfill and status == "indexed":
            conditions.append(
                "fi.first_indexed_at >= now() - interval '7 days'"
            )

        where = " AND ".join(conditions)
        sql = text(f"""
            SELECT fi.object_id, fi.ein, fi.tax_period,
                   fi.xml_batch_id, fi.filing_year, fi.s3_xml_key
            FROM lava_corpus.filing_index fi
            WHERE {where}
            ORDER BY fi.filing_year, fi.xml_batch_id, fi.object_id
        """)

        with engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            {
                "object_id": r[0],
                "ein": r[1],
                "tax_period": r[2],
                "xml_batch_id": r[3],
                "filing_year": r[4],
                "s3_xml_key": r[5],
            }
            for r in rows
        ]

    def _reset_errors(self, engine, ein_filter: str | None, backfill: bool):
        conditions = [
            "status = 'error'",
            "ein IN (SELECT ein FROM lava_corpus.nonprofits_seed)",
        ]
        params = {}
        if ein_filter:
            conditions.append("ein = :ein")
            params["ein"] = ein_filter
        if not backfill:
            conditions.append(
                "first_indexed_at >= now() - interval '7 days'"
            )

        where = " AND ".join(conditions)
        with engine.begin() as conn:
            result = conn.execute(text(f"""
                UPDATE lava_corpus.filing_index
                SET status = 'indexed', error_message = NULL, s3_xml_key = NULL
                WHERE {where}
            """), params)
            if result.rowcount > 0:
                self.stdout.write(
                    f"Reset {result.rowcount} error filings to indexed"
                )

    def _process_download_pass(
        self, engine, archive: S3990Archive, filings: list[dict], stats: dict
    ):
        batches: dict[tuple[int, str], list[dict]] = {}
        for f in filings:
            key = (f["filing_year"], f["xml_batch_id"])
            batches.setdefault(key, []).append(f)

        self.stdout.write(
            f"Download pass: {len(filings)} filings in {len(batches)} batches"
        )

        last_download = 0.0

        for (year, batch_id), batch_filings in batches.items():
            if not _BATCH_ID_RE.match(batch_id):
                for f in batch_filings:
                    _record_filing_error(
                        engine, f["object_id"], ErrorCode.ZIP_DOWNLOAD_FAILED,
                        "Invalid batch_id format"
                    )
                    stats["errors"] += 1
                continue

            if not archive.zip_exists(year, batch_id):
                elapsed = time.monotonic() - last_download
                if elapsed < _DOWNLOAD_DELAY:
                    time.sleep(_DOWNLOAD_DELAY - elapsed)

                try:
                    checksum = _download_zip_to_s3(year, batch_id, archive)
                    last_download = time.monotonic()
                except Exception as e:
                    log.error("Zip download failed %s: %s", batch_id, e)
                    for f in batch_filings:
                        _record_filing_error(
                            engine, f["object_id"],
                            ErrorCode.ZIP_DOWNLOAD_FAILED,
                            str(type(e).__name__)
                        )
                        stats["errors"] += 1
                    continue

                if checksum is None:
                    for f in batch_filings:
                        _record_filing_error(
                            engine, f["object_id"],
                            ErrorCode.ZIP_DOWNLOAD_FAILED,
                            "404 from IRS"
                        )
                        stats["errors"] += 1
                    continue

            self._extract_batch(
                engine, archive, year, batch_id, batch_filings, stats
            )

    def _extract_batch(
        self,
        engine,
        archive: S3990Archive,
        year: int,
        batch_id: str,
        filings: list[dict],
        stats: dict,
    ):
        try:
            zip_stream = archive.open_zip(year, batch_id)
        except Exception as e:
            log.error("Failed to open zip %s from S3: %s", batch_id, e)
            for f in filings:
                _record_filing_error(
                    engine, f["object_id"], ErrorCode.ZIP_CORRUPT,
                    str(type(e).__name__)
                )
                stats["errors"] += 1
            return

        try:
            zf = zipfile.ZipFile(zip_stream)
        except zipfile.BadZipFile:
            for f in filings:
                _record_filing_error(
                    engine, f["object_id"], ErrorCode.ZIP_CORRUPT,
                    "BadZipFile"
                )
                stats["errors"] += 1
            return

        with zf:
            if len(zf.namelist()) > _MAX_MEMBERS_PER_ZIP:
                for f in filings:
                    _record_filing_error(
                        engine, f["object_id"], ErrorCode.ZIP_BOMB_DETECTED,
                        "Member count exceeds limit"
                    )
                    stats["errors"] += 1
                return

            cumulative_extracted = 0

            for filing in filings:
                oid = filing["object_id"]
                ein = filing["ein"]

                nested_name = f"{batch_id}/{oid}_public.xml"
                flat_name = f"{oid}_public.xml"

                info = None
                member_name = None
                try:
                    info = zf.getinfo(nested_name)
                    member_name = nested_name
                except KeyError:
                    try:
                        info = zf.getinfo(flat_name)
                        member_name = flat_name
                    except KeyError:
                        _record_filing_error(
                            engine, oid, ErrorCode.ZIP_MEMBER_MISSING,
                            f"Tried {oid}_public.xml"
                        )
                        stats["errors"] += 1
                        continue

                if not _MEMBER_NAME_RE.match(info.filename):
                    _record_filing_error(
                        engine, oid, ErrorCode.ZIP_MEMBER_INVALID_NAME, ""
                    )
                    stats["errors"] += 1
                    continue

                compress_size = max(info.compress_size, 1)
                if info.file_size / compress_size > _BOMB_RATIO:
                    _record_filing_error(
                        engine, oid, ErrorCode.ZIP_BOMB_DETECTED,
                        f"Ratio {info.file_size / compress_size:.0f}:1"
                    )
                    stats["errors"] += 1
                    continue

                if info.file_size > _MAX_MEMBER_SIZE:
                    _record_filing_error(
                        engine, oid, ErrorCode.ZIP_MEMBER_TOO_LARGE,
                        f"{info.file_size} bytes"
                    )
                    stats["errors"] += 1
                    continue

                cumulative_extracted += info.file_size
                if cumulative_extracted > _MAX_BATCH_EXTRACTED:
                    _record_filing_error(
                        engine, oid, ErrorCode.BATCH_EXTRACT_LIMIT, ""
                    )
                    stats["errors"] += 1
                    continue

                try:
                    xml_bytes = zf.read(member_name)
                except Exception:
                    _record_filing_error(
                        engine, oid, ErrorCode.ZIP_CORRUPT,
                        "Read failed"
                    )
                    stats["errors"] += 1
                    continue

                if len(xml_bytes) > _MAX_MEMBER_SIZE:
                    _record_filing_error(
                        engine, oid, ErrorCode.ZIP_BOMB_DETECTED,
                        "Actual size exceeds limit"
                    )
                    stats["errors"] += 1
                    continue

                try:
                    s3_key = archive.upload_xml(ein, oid, xml_bytes)
                except Exception as e:
                    _record_filing_error(
                        engine, oid, ErrorCode.S3_UPLOAD_FAILED,
                        type(e).__name__
                    )
                    stats["errors"] += 1
                    continue

                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE lava_corpus.filing_index
                        SET status = 'downloaded',
                            s3_xml_key = :s3_key
                        WHERE object_id = :oid AND status = 'indexed'
                    """), {"s3_key": s3_key, "oid": oid})

                stats["downloaded"] += 1

    def _process_parse_pass(
        self, engine, archive: S3990Archive, filings: list[dict], stats: dict
    ):
        self.stdout.write(f"Parse pass: {len(filings)} filings")

        for filing in filings:
            oid = filing["object_id"]
            ein = filing["ein"]
            s3_key = filing["s3_xml_key"]

            if not s3_key:
                s3_key = f"xml/{ein}/{oid}.xml"

            try:
                xml_bytes = archive.read_xml(s3_key)
            except Exception:
                _record_filing_error(
                    engine, oid, ErrorCode.S3_OBJECT_MISSING,
                    "Could not read XML from S3"
                )
                stats["errors"] += 1
                continue

            result = _parse_xml_with_timeout(xml_bytes)
            if result is None:
                _record_filing_error(
                    engine, oid, ErrorCode.XML_PARSE_TIMEOUT, ""
                )
                stats["errors"] += 1
                continue

            try:
                if hasattr(result, "people") and result.people is not None:
                    pass
                else:
                    result = parse_990_xml(xml_bytes)
            except Exception as e:
                _record_filing_error(
                    engine, oid, ErrorCode.XML_PARSE_FAILED,
                    type(e).__name__
                )
                stats["errors"] += 1
                continue

            self._upsert_people_and_mark_parsed(engine, result, filing)
            stats["parsed"] += 1

    def _upsert_people_and_mark_parsed(self, engine, result, filing: dict):
        oid = filing["object_id"]

        upsert_sql = text("""
            INSERT INTO lava_corpus.people
                (ein, tax_period, object_id, person_name, title, person_type,
                 avg_hours_per_week, reportable_comp, related_org_comp, other_comp,
                 base_comp, bonus, other_reportable, deferred_comp,
                 nontaxable_benefits, total_comp_sch_j,
                 services_desc, is_officer, is_director, is_key_employee,
                 is_highest_comp, is_former)
            VALUES
                (:ein, :tax_period, :object_id, :person_name, :title, :person_type,
                 :avg_hours_per_week, :reportable_comp, :related_org_comp, :other_comp,
                 :base_comp, :bonus, :other_reportable, :deferred_comp,
                 :nontaxable_benefits, :total_comp_sch_j,
                 :services_desc, :is_officer, :is_director, :is_key_employee,
                 :is_highest_comp, :is_former)
            ON CONFLICT (ein, object_id, person_name, person_type) DO NOTHING
        """)

        with engine.begin() as conn:
            if result.people:
                meta = result.metadata
                for p in result.people:
                    name = unicodedata.normalize("NFC", p.person_name.strip())
                    conn.execute(upsert_sql, {
                        "ein": meta.ein,
                        "tax_period": meta.tax_period,
                        "object_id": oid,
                        "person_name": name,
                        "title": p.title,
                        "person_type": p.person_type,
                        "avg_hours_per_week": (
                            float(p.avg_hours_per_week)
                            if p.avg_hours_per_week is not None else None
                        ),
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
                    })

            conn.execute(text("""
                UPDATE lava_corpus.filing_index
                SET status = 'parsed',
                    parsed_at = now(),
                    return_ts = :return_ts,
                    is_amended = :is_amended,
                    error_message = NULL
                WHERE object_id = :oid
            """), {
                "oid": oid,
                "return_ts": result.metadata.return_ts,
                "is_amended": result.metadata.is_amended,
            })
