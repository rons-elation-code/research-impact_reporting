"""TEOS index CSV downloader and filter (Spec 0026, updated Spec 0030).

Downloads the IRS TEOS index CSV for a given year, filters to
RETURN_TYPE='990' and matching EINs, inserts into filing_index.

Supports both 9-column (2017-2023) and 10-column (2024+) CSV formats.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass

import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

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


@dataclass
class IndexStats:
    rows_scanned: int = 0
    rows_matched: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0


def _load_ein_set(
    engine: Engine,
    *,
    state: str | None = None,
    ein: str | None = None,
    limit: int | None = None,
) -> set[str]:
    if ein:
        return {ein}

    with engine.connect() as conn:
        if state:
            rows = conn.execute(
                text(
                    "SELECT ein FROM lava_corpus.nonprofits_seed "
                    "WHERE state = :state"
                ),
                {"state": state},
            ).fetchall()
        else:
            raise ValueError("Must specify --state or --ein")

    ein_set = {r[0] for r in rows}

    if limit and len(ein_set) > limit:
        ein_set = set(list(ein_set)[:limit])

    return ein_set


def download_and_filter_index(
    *,
    engine: Engine,
    year: int,
    state: str | None = None,
    ein: str | None = None,
    limit: int | None = None,
) -> IndexStats:
    """Download TEOS index CSV for year, filter to our EINs, insert into filing_index."""
    stats = IndexStats()

    ein_set = _load_ein_set(engine, state=state, ein=ein, limit=limit)
    if not ein_set:
        log.warning("No EINs to process for year %d", year)
        return stats

    log.info(
        "Downloading TEOS index for %d (%d target EINs)", year, len(ein_set),
    )

    url = TEOS_INDEX_URL.format(year=year)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    _INSERT_SQL = text("""
        INSERT INTO lava_corpus.filing_index
            (object_id, ein, tax_period, return_type, sub_date,
             taxpayer_name, xml_batch_id, filing_year, run_id)
        VALUES
            (:object_id, :ein, :tax_period, :return_type, :sub_date,
             :taxpayer_name, :xml_batch_id, :filing_year, :run_id)
        ON CONFLICT (object_id) DO NOTHING
    """)

    reader = csv.reader(
        io.StringIO(resp.content.decode("utf-8", errors="replace"))
    )

    header = next(reader, None)
    if header is None:
        log.warning("Empty index CSV for year %d", year)
        return stats

    with engine.begin() as conn:
        for row in reader:
            stats.rows_scanned += 1
            if len(row) < 9:
                continue

            return_type = row[_COL_RETURN_TYPE].strip()
            if return_type != "990":
                continue

            row_ein = row[_COL_EIN].strip()
            if row_ein not in ein_set:
                continue

            if not _EIN_RE.match(row_ein):
                continue

            object_id = row[_COL_OBJECT_ID].strip()
            if not _OBJECT_ID_RE.match(object_id):
                continue

            xml_batch_id = (
                row[_COL_XML_BATCH_ID].strip() if len(row) > 9 else None
            )
            if xml_batch_id and not _BATCH_ID_RE.match(xml_batch_id):
                xml_batch_id = None

            stats.rows_matched += 1

            result = conn.execute(_INSERT_SQL, {
                "object_id": object_id,
                "ein": row_ein,
                "tax_period": row[_COL_TAX_PERIOD].strip(),
                "return_type": return_type,
                "sub_date": row[_COL_SUB_DATE].strip() or None,
                "taxpayer_name": row[_COL_TAXPAYER_NAME].strip() or None,
                "xml_batch_id": xml_batch_id or None,
                "filing_year": year,
                "run_id": None,
            })

            if result.rowcount > 0:
                stats.rows_inserted += 1
            else:
                stats.rows_skipped += 1

    log.info(
        "Year %d: scanned=%d matched=%d inserted=%d skipped=%d",
        year, stats.rows_scanned, stats.rows_matched,
        stats.rows_inserted, stats.rows_skipped,
    )
    return stats
