"""CLI entry point for 990 Leadership & Contractor Intelligence (Spec 0026).

Enriches seeded nonprofits with leadership, key employee, and contractor
data from IRS 990 XML filings (TEOS bulk download).

Usage:
    python3 -m lavandula.nonprofits.tools.enrich_990 --state NY --years 2020,2021,2022,2023,2024
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
from pathlib import Path
from uuid import uuid4

from lavandula.common.db import make_app_engine
from lavandula.nonprofits.teos_download import process_filings
from lavandula.nonprofits.teos_index import download_and_filter_index

log = logging.getLogger(__name__)

_EIN_RE = re.compile(r"^\d{9}$")
_STATE_RE = re.compile(r"^[A-Z]{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")


def _validate_ein(value: str) -> str:
    if not _EIN_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"EIN must be exactly 9 digits, got {value!r}"
        )
    return value


def _validate_state(value: str) -> str:
    if not _STATE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"State must be exactly 2 uppercase letters, got {value!r}"
        )
    return value


def _validate_years(value: str) -> list[int]:
    parts = [p.strip() for p in value.split(",")]
    years = []
    for p in parts:
        if not _YEAR_RE.match(p):
            raise argparse.ArgumentTypeError(
                f"Year must be 4 digits, got {p!r}"
            )
        years.append(int(p))
    return years


def _validate_cache_dir(value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_symlink():
        raise argparse.ArgumentTypeError(
            f"Cache directory must not be a symlink: {value}"
        )
    p = p.resolve()
    if not p.is_dir():
        raise argparse.ArgumentTypeError(
            f"Cache directory does not exist: {value}"
        )
    return p


def _default_years() -> list[int]:
    current = dt.date.today().year
    return list(range(current - 4, current + 1))


def _log_cache_size(cache_dir: Path) -> None:
    total = sum(f.stat().st_size for f in cache_dir.glob("*.zip"))
    if total > 0:
        log.info(
            "Cache dir %s: %d zip files, %.1f GB total",
            cache_dir,
            sum(1 for _ in cache_dir.glob("*.zip")),
            total / (1024 ** 3),
        )
    else:
        log.info("Cache dir %s: empty", cache_dir)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Enrich nonprofits with 990 leadership & contractor data",
    )
    ap.add_argument(
        "--state", type=_validate_state,
        help="Filter EINs to orgs in this state (from nonprofits_seed)",
    )
    ap.add_argument(
        "--years", type=_validate_years,
        help="Comma-separated filing years (default: last 5 years)",
    )
    ap.add_argument(
        "--limit", type=int,
        help="Max orgs (EINs) to process",
    )
    ap.add_argument(
        "--ein", type=_validate_ein,
        help="Process a single EIN (bypasses --state)",
    )
    ap.add_argument(
        "--cache-dir", type=_validate_cache_dir,
        help="Directory for cached zip files (default: ~/.lavandula/990-cache/)",
    )
    ap.add_argument(
        "--skip-download", action="store_true",
        help="Parse only from cached files (offline mode)",
    )
    ap.add_argument(
        "--reparse", action="store_true",
        help="Re-parse previously parsed filings",
    )

    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.state and not args.ein:
        ap.error("Must specify --state or --ein")

    years = args.years or _default_years()

    if args.cache_dir:
        cache_dir = args.cache_dir
    else:
        cache_dir = Path.home() / ".lavandula" / "990-cache"
        if cache_dir.is_symlink():
            log.error("Default cache dir is a symlink — aborting")
            sys.exit(1)
        cache_dir.mkdir(parents=True, exist_ok=True)

    _log_cache_size(cache_dir)

    run_id = str(uuid4())
    log.info("Run ID: %s", run_id)

    engine = make_app_engine()

    ein_set: set[str] | None = None
    if args.ein:
        ein_set = {args.ein}
    elif args.state:
        from lavandula.nonprofits.teos_index import _load_ein_set
        ein_set = _load_ein_set(engine, state=args.state, limit=args.limit)
        log.info("Loaded %d EINs for state %s", len(ein_set), args.state)

    for year in years:
        try:
            stats = download_and_filter_index(
                engine=engine,
                year=year,
                state=args.state if not args.ein else None,
                ein=args.ein,
                limit=args.limit,
            )
            log.info(
                "Index %d: inserted=%d matched=%d",
                year, stats.rows_inserted, stats.rows_matched,
            )
        except Exception:
            log.exception("Failed to process index for year %d", year)

    proc_stats = process_filings(
        engine=engine,
        cache_dir=cache_dir,
        skip_download=args.skip_download,
        reparse=args.reparse,
        run_id=run_id,
        ein_set=ein_set,
        filing_years=years,
    )

    log.info(
        "Done: processed=%d parsed=%d skipped=%d errors=%d people=%d "
        "zips_downloaded=%d zips_cached=%d",
        proc_stats.filings_processed,
        proc_stats.filings_parsed,
        proc_stats.filings_skipped,
        proc_stats.filings_error,
        proc_stats.people_upserted,
        proc_stats.zips_downloaded,
        proc_stats.zips_cached,
    )


if __name__ == "__main__":
    main()
