"""CLI entry point for 990 Leadership & Contractor Intelligence (Spec 0026).

DEPRECATED: This module now delegates to the new management commands
introduced in Spec 0030. The --index-only path calls load_990_index,
and the --parse-only path calls process_990_auto. The combined mode
runs both sequentially.

The old local-cache-based code path is retained for the combined
(non-index-only, non-parse-only) mode as a fallback until the S3
infrastructure is fully validated.

Usage:
    python3 -m lavandula.nonprofits.tools.enrich_990 --state NY --years 2024
    python3 -m lavandula.nonprofits.tools.enrich_990 --ein 131624241 --index-only
    python3 -m lavandula.nonprofits.tools.enrich_990 --ein 131624241 --parse-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import subprocess
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

_MANAGE_PY = Path(__file__).resolve().parents[3] / "dashboard" / "manage.py"


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


def _default_years() -> list[int]:
    current = dt.date.today().year
    return list(range(current - 4, current + 1))


def _run_management_command(cmd_args: list[str]) -> int:
    """Run a Django management command via subprocess."""
    full_cmd = [sys.executable, str(_MANAGE_PY)] + cmd_args
    log.info("Running: %s", " ".join(full_cmd))
    result = subprocess.run(
        full_cmd,
        cwd=str(_MANAGE_PY.parent),
    )
    return result.returncode


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
        "--cache-dir", type=str, default=None,
        help="(Deprecated) Directory for cached zip files",
    )
    ap.add_argument(
        "--skip-download", action="store_true",
        help="Parse only from cached files (offline mode)",
    )
    ap.add_argument(
        "--reparse", action="store_true",
        help="Re-parse previously parsed filings",
    )

    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--index-only", action="store_true",
        help="Run only index download via load_990_index",
    )
    mode_group.add_argument(
        "--parse-only", action="store_true",
        help="Run only parse via process_990_auto",
    )

    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.state and not args.ein:
        ap.error("Must specify --state or --ein")

    years = args.years or _default_years()

    if args.index_only:
        cmd = ["load_990_index"]
        if args.ein:
            cmd.extend(["--ein", args.ein])
        if args.years:
            cmd.extend(["--years", ",".join(str(y) for y in years)])
        rc = _run_management_command(cmd)
        sys.exit(rc)

    if args.parse_only:
        cmd = ["process_990_auto"]
        if args.ein:
            cmd.extend(["--ein", args.ein])
        if args.reparse:
            cmd.append("--reparse")
        rc = _run_management_command(cmd)
        sys.exit(rc)

    # Combined mode: run index then parse via new commands
    index_cmd = ["load_990_index"]
    if args.ein:
        index_cmd.extend(["--ein", args.ein])
    if args.years:
        index_cmd.extend(["--years", ",".join(str(y) for y in years)])

    rc = _run_management_command(index_cmd)
    if rc != 0:
        log.error("Index command failed with exit code %d", rc)
        sys.exit(rc)

    parse_cmd = ["process_990_auto"]
    if args.ein:
        parse_cmd.extend(["--ein", args.ein])
    if args.reparse:
        parse_cmd.append("--reparse")

    rc = _run_management_command(parse_cmd)
    sys.exit(rc)


if __name__ == "__main__":
    main()
