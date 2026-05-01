"""Resolve xml_batch_id for 2017-2023 filings via zip central directory scan (Spec 0030).

For filings where xml_batch_id IS NULL, probes IRS batch zips to find which
batch contains each object_id. Uses HTTP Range requests when supported to read
only the zip central directory, falling back to full download otherwise.

Usage:
    python3 manage.py resolve_990_batches               # all years with NULL batch IDs
    python3 manage.py resolve_990_batches --years 2022  # specific year
"""
from __future__ import annotations

import io
import ipaddress
import logging
import re
import socket
import struct
import tempfile
import time
import zipfile
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand
from sqlalchemy import text

from lavandula.common.db import make_app_engine

log = logging.getLogger(__name__)

TEOS_ZIP_URL = (
    "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch_id}.zip"
)

_BATCH_ID_RE = re.compile(r"^\d{4}_TEOS_XML_(0[1-9]|1[0-2])[A-D]$", re.ASCII)
_MEMBER_OID_RE = re.compile(r"^(?:[\w]+/)?(\d+)_public\.xml$", re.ASCII)
_LOCK_KEY = "990-family"

_EOCD_SIZE = 22
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_EOCD64_SIGNATURE = b"PK\x06\x06"
_MAX_TAIL_READ = 65536


def _resolve_and_check_host(hostname: str) -> None:
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")
    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"Hostname resolves to private IP: {ip}")


def _enumerate_batch_ids(year: int) -> list[str]:
    """Generate all possible batch IDs for a year."""
    ids = []
    for month in range(1, 13):
        for suffix in "ABCD":
            ids.append(f"{year}_TEOS_XML_{month:02d}{suffix}")
    return ids


def _probe_range_support(url: str) -> bool:
    """Check if server supports HTTP Range requests."""
    resp = requests.head(url, timeout=30, allow_redirects=False)
    if resp.status_code in (301, 302, 303, 307, 308):
        return False
    if resp.status_code != 200:
        return False
    accept_ranges = resp.headers.get("Accept-Ranges", "").lower()
    return accept_ranges == "bytes"


def _get_content_length(url: str) -> int | None:
    """Get file size via HEAD request."""
    resp = requests.head(url, timeout=30, allow_redirects=False)
    if resp.status_code != 200:
        return None
    cl = resp.headers.get("Content-Length")
    return int(cl) if cl else None


def _read_central_directory_via_range(url: str) -> list[str] | None:
    """Read zip central directory using Range requests. Returns member filenames."""
    file_size = _get_content_length(url)
    if file_size is None:
        return None

    tail_size = min(_MAX_TAIL_READ, file_size)
    start = file_size - tail_size
    resp = requests.get(
        url,
        headers={"Range": f"bytes={start}-{file_size - 1}"},
        timeout=60,
        allow_redirects=False,
    )

    if resp.status_code != 206:
        return None

    if len(resp.content) > _MAX_TAIL_READ:
        return None

    tail = resp.content

    eocd_offset = tail.rfind(_EOCD_SIGNATURE)
    if eocd_offset == -1:
        return None

    cd_offset = None
    cd_size = None

    loc_offset = tail.rfind(_EOCD64_LOCATOR_SIGNATURE)
    if loc_offset != -1 and loc_offset + 20 <= len(tail):
        eocd64_abs_offset = struct.unpack_from("<Q", tail, loc_offset + 8)[0]
        if eocd64_abs_offset >= start:
            eocd64_rel = eocd64_abs_offset - start
            if (eocd64_rel + 56 <= len(tail) and
                    tail[eocd64_rel:eocd64_rel + 4] == _EOCD64_SIGNATURE):
                cd_size = struct.unpack_from("<Q", tail, eocd64_rel + 40)[0]
                cd_offset = struct.unpack_from("<Q", tail, eocd64_rel + 48)[0]

    if cd_offset is None:
        cd_size = struct.unpack_from("<I", tail, eocd_offset + 12)[0]
        cd_offset = struct.unpack_from("<I", tail, eocd_offset + 16)[0]
        if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
            return None

    cd_resp = requests.get(
        url,
        headers={"Range": f"bytes={cd_offset}-{cd_offset + cd_size - 1}"},
        timeout=120,
        allow_redirects=False,
    )
    if cd_resp.status_code != 206:
        return None

    cd_data = cd_resp.content
    filenames = []
    pos = 0
    while pos + 46 <= len(cd_data):
        sig = cd_data[pos:pos + 4]
        if sig != b"PK\x01\x02":
            break
        name_len = struct.unpack_from("<H", cd_data, pos + 28)[0]
        extra_len = struct.unpack_from("<H", cd_data, pos + 30)[0]
        comment_len = struct.unpack_from("<H", cd_data, pos + 32)[0]
        name_start = pos + 46
        name_end = name_start + name_len
        if name_end > len(cd_data):
            break
        filename = cd_data[name_start:name_end].decode("utf-8", errors="replace")
        filenames.append(filename)
        pos = name_end + extra_len + comment_len

    return filenames


def _read_central_directory_full_download(url: str) -> list[str] | None:
    """Download full zip to temp file and read member list."""
    resp = requests.get(url, stream=True, timeout=300, allow_redirects=False)
    if resp.status_code != 200:
        return None

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp:
        for chunk in resp.iter_content(chunk_size=65536):
            tmp.write(chunk)
        tmp.flush()

        try:
            with zipfile.ZipFile(tmp.name) as zf:
                return zf.namelist()
        except zipfile.BadZipFile:
            return None


def _extract_object_ids(filenames: list[str]) -> dict[str, str]:
    """Map object_id -> member_name from zip member filenames."""
    mapping = {}
    for fn in filenames:
        m = _MEMBER_OID_RE.match(fn)
        if m:
            mapping[m.group(1)] = fn
    return mapping


class Command(BaseCommand):
    help = "Resolve xml_batch_id for filings where it is NULL (2017-2023)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--years",
            type=str,
            default=None,
            help="Comma-separated years to resolve (default: all with NULL batch IDs)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without updating the database",
        )

    def handle(self, *args, **options):
        engine = make_app_engine()

        if options["years"]:
            years = [int(y.strip()) for y in options["years"].split(",")]
        else:
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT DISTINCT filing_year
                    FROM lava_corpus.filing_index
                    WHERE xml_batch_id IS NULL AND status != 'batch_unresolvable'
                    ORDER BY filing_year
                """)).fetchall()
                years = [r[0] for r in rows]

        if not years:
            self.stdout.write("No years with unresolved batch IDs.")
            return

        self.stdout.write(f"Resolving batch IDs for years: {years}")

        with engine.connect() as conn:
            conn.execute(
                text("SELECT pg_advisory_lock(hashtext(:key))"),
                {"key": _LOCK_KEY},
            )
            conn.commit()

        try:
            probe_url = TEOS_ZIP_URL.format(
                year=years[0],
                batch_id=f"{years[0]}_TEOS_XML_01A",
            )
            parsed = urlparse(probe_url)
            _resolve_and_check_host(parsed.hostname)
            range_supported = _probe_range_support(probe_url)
            self.stdout.write(
                f"HTTP Range support: {'yes' if range_supported else 'no'}"
            )

            for year in sorted(years):
                self._resolve_year(
                    engine, year, range_supported, options["dry_run"]
                )
        finally:
            with engine.connect() as conn:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"),
                    {"key": _LOCK_KEY},
                )
                conn.commit()

    def _resolve_year(
        self, engine, year: int, range_supported: bool, dry_run: bool
    ):
        self.stdout.write(f"\nYear {year}:")

        with engine.connect() as conn:
            unresolved = conn.execute(text("""
                SELECT COUNT(*)
                FROM lava_corpus.filing_index
                WHERE filing_year = :year AND xml_batch_id IS NULL
                  AND status != 'batch_unresolvable'
            """), {"year": year}).scalar()

        if unresolved == 0:
            self.stdout.write(f"  No unresolved filings for {year}")
            return

        self.stdout.write(f"  {unresolved} filings need batch resolution")

        batch_ids = _enumerate_batch_ids(year)
        total_resolved = 0

        for batch_id in batch_ids:
            url = TEOS_ZIP_URL.format(year=year, batch_id=batch_id)

            head_resp = requests.head(url, timeout=30, allow_redirects=False)
            if head_resp.status_code != 200:
                continue

            self.stdout.write(f"  Scanning {batch_id}...")

            if range_supported:
                filenames = _read_central_directory_via_range(url)
            else:
                filenames = _read_central_directory_full_download(url)

            if filenames is None:
                self.stdout.write(f"    Failed to read central directory")
                continue

            oid_map = _extract_object_ids(filenames)
            self.stdout.write(
                f"    {len(oid_map)} XML members found"
            )

            if not oid_map or dry_run:
                if dry_run and oid_map:
                    total_resolved += len(oid_map)
                continue

            oid_list = list(oid_map.keys())
            with engine.begin() as conn:
                result = conn.execute(text("""
                    UPDATE lava_corpus.filing_index
                    SET xml_batch_id = :batch_id
                    WHERE object_id = ANY(:oids)
                      AND xml_batch_id IS NULL
                """), {"batch_id": batch_id, "oids": oid_list})
                total_resolved += result.rowcount

            time.sleep(1)

        if not dry_run:
            with engine.begin() as conn:
                result = conn.execute(text("""
                    UPDATE lava_corpus.filing_index
                    SET status = 'batch_unresolvable'
                    WHERE filing_year = :year
                      AND xml_batch_id IS NULL
                      AND status = 'indexed'
                """), {"year": year})
                unresolvable = result.rowcount

            self.stdout.write(
                f"  Year {year}: resolved={total_resolved} "
                f"unresolvable={unresolvable}"
            )
        else:
            self.stdout.write(
                f"  Year {year}: would resolve ~{total_resolved} (dry run)"
            )
