"""Bulk-load IRS TEOS filing index CSVs into filing_index (Spec 0030).

Downloads each year's index CSV, filters to RETURN_TYPE='990', and bulk-inserts
into filing_index with ON CONFLICT handling. Supports 9-column (2017-2023) and
10-column (2024+) formats.

Usage:
    python3 manage.py load_990_index                  # all available years
    python3 manage.py load_990_index --years 2024,2025
    python3 manage.py load_990_index --current-year   # nightly cron
    python3 manage.py load_990_index --ein 131624241  # single EIN filter
"""
from __future__ import annotations

import csv
import datetime
import io
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand
from sqlalchemy import text

from lavandula.common.db import make_app_engine

log = logging.getLogger(__name__)

TEOS_INDEX_URL = (
    "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
)

_COL_RETURN_ID = 0
_COL_FILING_TYPE = 1
_COL_EIN = 2
_COL_TAX_PERIOD = 3
_COL_SUB_DATE = 4
_COL_TAXPAYER_NAME = 5
_COL_RETURN_TYPE = 6
_COL_DLN = 7
_COL_OBJECT_ID = 8
_COL_XML_BATCH_ID = 9

_EIN_RE = re.compile(r"^\d{9}$", re.ASCII)
_OBJECT_ID_RE = re.compile(r"^\d+$", re.ASCII)
_BATCH_ID_RE = re.compile(r"^\d{4}_TEOS_XML_(0[1-9]|1[0-2])[A-D]$", re.ASCII)

_MAX_CSV_BYTES = 200 * 1024 * 1024  # 200 MB
_BATCH_SIZE = 5000
_MIN_YEAR = 2017
_LOCK_KEY = "990-family"


def _resolve_and_check_host(hostname: str) -> None:
    """Resolve hostname and reject private/link-local IPs (SSRF defense)."""
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")
    for family, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(
                f"Hostname {hostname} resolves to private IP {ip}"
            )


def _safe_download_csv(url: str) -> io.StringIO:
    """Download CSV with size cap and SSRF protections."""
    parsed = urlparse(url)
    if parsed.hostname != "apps.irs.gov":
        raise ValueError(f"Unexpected hostname in URL: {parsed.hostname}")
    if parsed.scheme != "https":
        raise ValueError(f"Non-HTTPS scheme: {parsed.scheme}")

    _resolve_and_check_host(parsed.hostname)

    resp = requests.get(url, stream=True, timeout=120, allow_redirects=False)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "")
        redirect_parsed = urlparse(location)
        if redirect_parsed.hostname != "apps.irs.gov":
            raise ValueError(
                f"Redirect to non-IRS host: {redirect_parsed.hostname}"
            )
        if redirect_parsed.scheme != "https":
            raise ValueError(f"Redirect to non-HTTPS: {redirect_parsed.scheme}")
        _resolve_and_check_host(redirect_parsed.hostname)
        resp = requests.get(location, stream=True, timeout=120, allow_redirects=False)

    if resp.status_code == 404:
        return None

    resp.raise_for_status()

    chunks = []
    bytes_read = 0
    for chunk in resp.iter_content(chunk_size=65536):
        bytes_read += len(chunk)
        if bytes_read > _MAX_CSV_BYTES:
            raise ValueError(
                f"CSV exceeds size limit ({_MAX_CSV_BYTES} bytes)"
            )
        chunks.append(chunk)

    raw = b"".join(chunks)
    return io.StringIO(raw.decode("utf-8", errors="replace"))


class Command(BaseCommand):
    help = "Bulk-load IRS TEOS 990 filing index into filing_index table"

    def add_arguments(self, parser):
        parser.add_argument(
            "--years",
            type=str,
            default=None,
            help="Comma-separated years to load (default: all 2017-current)",
        )
        parser.add_argument(
            "--current-year",
            action="store_true",
            help="Only load the current year (for nightly cron)",
        )
        parser.add_argument(
            "--ein",
            type=str,
            default=None,
            help="Only insert/update rows matching this EIN",
        )

    def handle(self, *args, **options):
        current_year = datetime.date.today().year

        if options["current_year"]:
            years = [current_year]
        elif options["years"]:
            years = []
            for y in options["years"].split(","):
                y = y.strip()
                if not re.match(r"^\d{4}$", y):
                    self.stderr.write(f"Invalid year: {y}")
                    return
                years.append(int(y))
        else:
            years = list(range(_MIN_YEAR, current_year + 1))

        ein_filter = options.get("ein")
        if ein_filter and not _EIN_RE.match(ein_filter):
            self.stderr.write(f"Invalid EIN: {ein_filter}")
            return

        engine = make_app_engine()

        lock_conn = engine.connect()
        lock_conn.execute(
            text("SELECT pg_advisory_lock(hashtext(:key))"),
            {"key": _LOCK_KEY},
        )
        lock_conn.commit()

        try:
            for year in sorted(years):
                self._load_year(engine, year, ein_filter)
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": _LOCK_KEY},
            )
            lock_conn.commit()
            lock_conn.close()

    def _load_year(self, engine, year: int, ein_filter: str | None):
        url = TEOS_INDEX_URL.format(year=year)
        self.stdout.write(f"Loading index for {year}...")

        t0 = time.monotonic()

        try:
            csv_data = _safe_download_csv(url)
        except ValueError as e:
            self.stderr.write(f"  Skipping {year}: {e}")
            return

        if csv_data is None:
            self.stdout.write(f"  {year}: 404 (not available)")
            return

        reader = csv.reader(csv_data)
        header = next(reader, None)
        if header is None:
            self.stdout.write(f"  {year}: empty CSV")
            return

        col_count = len(header)
        self.stdout.write(f"  {year}: {col_count}-column format")

        rows_scanned = 0
        rows_inserted = 0
        rows_updated = 0
        batch = []

        insert_sql = text("""
            INSERT INTO lava_corpus.filing_index
                (object_id, ein, tax_period, return_type, sub_date,
                 taxpayer_name, xml_batch_id, filing_year, status,
                 first_indexed_at, last_seen_at)
            VALUES
                (:object_id, :ein, :tax_period, :return_type, :sub_date,
                 :taxpayer_name, :xml_batch_id, :filing_year, 'indexed',
                 now(), now())
            ON CONFLICT (object_id) DO UPDATE SET
                last_seen_at = now()
            RETURNING (xmax = 0) AS inserted
        """)

        def flush_batch(conn, batch_rows):
            nonlocal rows_inserted, rows_updated
            for row_params in batch_rows:
                result = conn.execute(insert_sql, row_params)
                row = result.fetchone()
                if row and row[0]:
                    rows_inserted += 1
                else:
                    rows_updated += 1

        with engine.begin() as conn:
            for row in reader:
                rows_scanned += 1
                if len(row) < 9:
                    continue

                return_type = row[_COL_RETURN_TYPE].strip()
                if return_type != "990":
                    continue

                row_ein = row[_COL_EIN].strip()
                if not _EIN_RE.match(row_ein):
                    continue

                if ein_filter and row_ein != ein_filter:
                    continue

                object_id = row[_COL_OBJECT_ID].strip()
                if not _OBJECT_ID_RE.match(object_id):
                    continue

                xml_batch_id = (
                    row[_COL_XML_BATCH_ID].strip() if len(row) > 9 else None
                )
                if xml_batch_id and not _BATCH_ID_RE.match(xml_batch_id):
                    xml_batch_id = None

                batch.append({
                    "object_id": object_id,
                    "ein": row_ein,
                    "tax_period": row[_COL_TAX_PERIOD].strip(),
                    "return_type": return_type,
                    "sub_date": row[_COL_SUB_DATE].strip() or None,
                    "taxpayer_name": row[_COL_TAXPAYER_NAME].strip() or None,
                    "xml_batch_id": xml_batch_id or None,
                    "filing_year": year,
                })

                if len(batch) >= _BATCH_SIZE:
                    flush_batch(conn, batch)
                    batch = []

            if batch:
                flush_batch(conn, batch)

        duration = time.monotonic() - t0

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO lava_corpus.index_refresh_log
                    (filing_year, rows_scanned, rows_inserted, rows_skipped, duration_sec)
                VALUES
                    (:year, :scanned, :inserted, :skipped, :duration)
            """), {
                "year": year,
                "scanned": rows_scanned,
                "inserted": rows_inserted,
                "skipped": rows_updated,
                "duration": round(duration, 2),
            })

        self.stdout.write(
            f"  {year}: scanned={rows_scanned} inserted={rows_inserted} "
            f"updated={rows_updated} ({duration:.1f}s)"
        )
