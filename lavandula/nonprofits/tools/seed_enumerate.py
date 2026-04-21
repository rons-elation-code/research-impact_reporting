"""ProPublica enumerator for mid-market nonprofits.

Populates seeds.db with organizations matching the ICP filter,
skipping EINs already present so repeat runs expand the library
without re-doing work.

Usage:
    python -m lavandula.nonprofits.tools.seed_enumerate [OPTIONS]

Filters: states, NTEE majors, and revenue bounds are CLI-configurable.
Defaults: coastal states (CA NY MA WA OR CT NJ MD RI), NTEE A/B/E/P,
revenue $1M–$30M, target 100 new orgs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class OrgDetail:
    revenue: int | None
    ntee_code: str | None
    address: str | None
    zipcode: str | None
    subsection_code: int | None
    activity_codes: str | None
    classification_codes: str | None
    foundation_code: int | None
    ruling_date: str | None
    accounting_period: int | None


def _to_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_str(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_STATES = ("CA", "NY", "MA", "WA", "OR", "CT", "NJ", "MD", "RI")
DEFAULT_NTEE_MAJORS = ("A", "B", "E", "P")
DEFAULT_REV_MIN = 1_000_000
DEFAULT_REV_MAX = 30_000_000
DEFAULT_TARGET = 100
MAX_RESPONSE_BYTES = 1_024 * 1_024  # 1 MB

PROPUBLICA_SEARCH = "https://projects.propublica.org/nonprofits/api/v2/search.json"
PROPUBLICA_ORG = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
UA = (
    "Lavandula Design research "
    "(+https://lavanduladesign.com; crawler-contact@lavanduladesign.com)"
)
SLEEP_BETWEEN_CALLS = 0.35

_EIN_RE = re.compile(r"^\d{9}$")

# ── schema ────────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nonprofits_seed (
  ein                     TEXT PRIMARY KEY,
  name                    TEXT,
  address                 TEXT,
  city                    TEXT,
  state                   TEXT,
  zipcode                 TEXT,
  ntee_code               TEXT,
  revenue                 INTEGER,
  website_url             TEXT,
  website_candidates_json TEXT,
  discovered_at           TEXT,
  run_id                  TEXT
);
CREATE VIEW IF NOT EXISTS nonprofits AS
  SELECT ein, website_url, resolver_status FROM nonprofits_seed;
CREATE TABLE IF NOT EXISTS runs (
  run_id           TEXT PRIMARY KEY,
  started_at       TEXT,
  finished_at      TEXT,
  filters_json     TEXT,
  found_count      INTEGER,
  website_hit_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_seed_state ON nonprofits_seed(state);
CREATE INDEX IF NOT EXISTS idx_seed_website_null
  ON nonprofits_seed(ein) WHERE website_url IS NULL;
"""


# ── sentinel exceptions ───────────────────────────────────────────────────────
class _RateLimited(Exception):
    """429 retries exhausted — caller should commit + exit(0)."""


class _SkipPage(Exception):
    """Page cannot be parsed (oversized or bad JSON) — cursor does NOT advance."""


class _SkipPair(Exception):
    """5xx / network retries exhausted — skip this (state, ntee) pair."""


class _InfraError(Exception):
    """5 consecutive failures — caller should commit + exit(1)."""


# ── Step 1: schema migrations ─────────────────────────────────────────────────
def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations run after base schema creation."""
    existing_runs = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "last_page_scanned" not in existing_runs:
        conn.execute("ALTER TABLE runs ADD COLUMN last_page_scanned TEXT DEFAULT NULL")
    if "exit_reason" not in existing_runs:
        conn.execute("ALTER TABLE runs ADD COLUMN exit_reason TEXT DEFAULT NULL")
    existing_seed = {row[1] for row in conn.execute("PRAGMA table_info(nonprofits_seed)")}
    if "notes" not in existing_seed:
        conn.execute("ALTER TABLE nonprofits_seed ADD COLUMN notes TEXT DEFAULT NULL")
    for col, typedef in [
        ("address", "TEXT DEFAULT NULL"),
        ("zipcode", "TEXT DEFAULT NULL"),
        ("subsection_code", "INTEGER DEFAULT NULL"),
        ("activity_codes", "TEXT DEFAULT NULL"),
        ("classification_codes", "TEXT DEFAULT NULL"),
        ("foundation_code", "INTEGER DEFAULT NULL"),
        ("ruling_date", "TEXT DEFAULT NULL"),
        ("accounting_period", "INTEGER DEFAULT NULL"),
        ("resolver_status", "TEXT DEFAULT NULL"),
        ("resolver_confidence", "REAL DEFAULT NULL"),
        ("resolver_method", "TEXT DEFAULT NULL"),
        ("resolver_reason", "TEXT DEFAULT NULL"),
        ("website_candidates_json", "TEXT DEFAULT NULL"),
    ]:
        if col not in existing_seed:
            conn.execute(f"ALTER TABLE nonprofits_seed ADD COLUMN {col} {typedef}")
    conn.commit()


def ensure_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    _apply_migrations(conn)
    return conn


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ── Step 7: structured logging helper ─────────────────────────────────────────
def _finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    found: int,
    reason: str,
    exit_code: int,
) -> None:
    """Commit terminal run state and exit."""
    conn.execute(
        "UPDATE runs SET finished_at=?, found_count=?, exit_reason=? WHERE run_id=?",
        (iso_now(), found, reason, run_id),
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM nonprofits_seed").fetchone()[0]
    log.info("done total_added=%d db_rows=%d exit_reason=%s", found, total, reason)
    sys.exit(exit_code)


# ── Step 5: HTTP layer with retry ─────────────────────────────────────────────
def _parse_retry_after(header: str) -> int:
    """Parse Retry-After header (integer seconds or HTTP-date) → seconds."""
    if not header:
        return 0
    s = header.strip()
    try:
        return max(0, int(s))
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S GMT", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            then = dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
            return max(0, int((then - dt.datetime.now(dt.timezone.utc)).total_seconds()))
        except ValueError:
            continue
    return 0


# Delays between attempts (len = max_retries; len+1 = total attempts before giving up).
_DELAYS_429 = [1, 5]   # 3 total 429 attempts; after 3rd failure → _RateLimited
_DELAYS_5XX = [2]      # 2 total 5xx/network attempts; after 2nd failure → _SkipPair


def _fetch_with_retry(url: str, *, fail_counter: dict[str, int]) -> dict:
    """Fetch *url*, retrying on transient errors per the TICK-005 error table.

    Raises:
        _RateLimited  — 429 retries exhausted
        _SkipPage     — oversized or un-parseable response (cursor must not advance)
        _SkipPair     — 5xx/network retries exhausted (skip this state/ntee pair)
        _InfraError   — 5 consecutive failures (halt the run)
    """
    rate_attempt = 0
    server_attempt = 0

    while True:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    log.warning("large_response bytes=%d url=<redacted>", len(raw))
                    fail_counter["count"] += 1
                    if fail_counter["count"] >= 5:
                        raise _InfraError("5 consecutive failures")
                    raise _SkipPage("oversized response")
                # Success: reset consecutive-failure counter
                fail_counter["count"] = 0
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("json_parse_error bytes=%d url=<redacted>", len(raw))
                    fail_counter["count"] += 1
                    if fail_counter["count"] >= 5:
                        raise _InfraError("5 consecutive failures")
                    raise _SkipPage("json parse error")

        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                ra = _parse_retry_after(
                    exc.headers.get("Retry-After", "") if exc.headers else ""
                )
                rate_attempt += 1
                fail_counter["count"] += 1
                if fail_counter["count"] >= 5:
                    raise _InfraError("5 consecutive failures") from exc
                if rate_attempt > len(_DELAYS_429):
                    raise _RateLimited() from exc
                delay = max(_DELAYS_429[rate_attempt - 1], ra)
                log.warning("http_error status=429 attempt=%d url=<redacted>", rate_attempt)
                time.sleep(delay)
                continue

            if exc.code >= 500:
                server_attempt += 1
                fail_counter["count"] += 1
                if fail_counter["count"] >= 5:
                    raise _InfraError("5 consecutive failures") from exc
                if server_attempt > len(_DELAYS_5XX):
                    raise _SkipPair(f"http {exc.code}") from exc
                log.warning(
                    "http_error status=%d attempt=%d url=<redacted>", exc.code, server_attempt
                )
                time.sleep(_DELAYS_5XX[server_attempt - 1])
                continue

            # Non-retriable 4xx (except 429 handled above)
            fail_counter["count"] += 1
            if fail_counter["count"] >= 5:
                raise _InfraError("5 consecutive failures") from exc
            raise

        except (_SkipPage, _RateLimited, _InfraError):
            raise

        except OSError as exc:
            server_attempt += 1
            fail_counter["count"] += 1
            if fail_counter["count"] >= 5:
                raise _InfraError("5 consecutive failures") from exc
            if server_attempt > len(_DELAYS_5XX):
                raise _SkipPair(f"network error: {type(exc).__name__}") from exc
            log.warning(
                "network_error type=%s attempt=%d url=<redacted>",
                type(exc).__name__,
                server_attempt,
            )
            time.sleep(_DELAYS_5XX[server_attempt - 1])
            continue


def _fetch_org_revenue(
    ein: str, *, fail_counter: dict[str, int]
) -> OrgDetail | None:
    """Return OrgDetail from most recent 990, or None on HTTP/network failure."""
    url = PROPUBLICA_ORG.format(ein=ein)
    try:
        d = _fetch_with_retry(url, fail_counter=fail_counter)
    except (_SkipPage, _SkipPair):
        return None
    # _RateLimited and _InfraError propagate to the caller
    org = d.get("organization") or {}
    filings = d.get("filings_with_data") or []
    return OrgDetail(
        revenue=_to_int(filings[0]["totrevenue"]) if filings else None,
        ntee_code=_to_str(org.get("ntee_code")),
        address=_to_str(org.get("address")),
        zipcode=_to_str(org.get("zipcode")),
        subsection_code=_to_int(org.get("subsection_code")),
        activity_codes=_to_str(org.get("activity_codes")),
        classification_codes=_to_str(org.get("classification_codes")),
        foundation_code=_to_int(org.get("foundation_code")),
        ruling_date=_to_str(org.get("ruling_date")),
        accounting_period=_to_int(org.get("accounting_period")),
    )


# ── Step 3: filter mismatch guard ─────────────────────────────────────────────
def _check_filter_consistency(
    conn: sqlite3.Connection,
    states: list[str],
    ntee_majors: list[str],
    rev_min: int,
    rev_max: int,
) -> None:
    """Exit 2 if a previous run used different filters against this DB."""
    row = conn.execute(
        "SELECT filters_json FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return
    prev = json.loads(row[0])
    if sorted(prev.get("states", [])) != sorted(states):
        print("Filter mismatch: --states changed; use a different --db", file=sys.stderr)
        sys.exit(2)
    if sorted(prev.get("ntee_majors", [])) != sorted(ntee_majors):
        print("Filter mismatch: --ntee-majors changed; use a different --db", file=sys.stderr)
        sys.exit(2)
    if prev.get("rev_min") != rev_min or prev.get("rev_max") != rev_max:
        print("Filter mismatch: revenue bounds changed; use a different --db", file=sys.stderr)
        sys.exit(2)


# ── Step 4: cursor / checkpoint ───────────────────────────────────────────────
def _get_or_create_run(
    conn: sqlite3.Connection,
    states: list[str],
    ntee_majors: list[str],
    rev_min: int,
    rev_max: int,
) -> tuple[str, dict[str, int]]:
    """Return (run_id, cursor). Resume an incomplete run or create a fresh one.

    Cursor key: "{state}:{ntee_major}" → last successfully committed page number.
    Resume starts at cursor[key]+1 so the un-committed page is always re-fetched
    (idempotent via EIN PRIMARY KEY).
    """
    row = conn.execute(
        "SELECT run_id, last_page_scanned FROM runs "
        "WHERE finished_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        run_id: str = row[0]
        cursor: dict[str, int] = json.loads(row[1]) if row[1] else {}
        log.info("resume run_id=%s cursor_keys=%d", run_id, len(cursor))
        return run_id, cursor

    run_id = uuid.uuid4().hex[:10]
    filters = {
        "states": sorted(states),
        "ntee_majors": sorted(ntee_majors),
        "rev_min": rev_min,
        "rev_max": rev_max,
    }
    conn.execute(
        "INSERT INTO runs(run_id, started_at, filters_json, found_count) VALUES (?, ?, ?, 0)",
        (run_id, iso_now(), json.dumps(filters)),
    )
    conn.commit()
    log.info("new_run run_id=%s", run_id)
    return run_id, {}


# ── core enumeration ──────────────────────────────────────────────────────────
def enumerate_new_orgs(
    conn: sqlite3.Connection,
    *,
    target: int,
    states: list[str] | tuple[str, ...],
    ntee_majors: list[str] | tuple[str, ...],
    rev_min: int,
    rev_max: int,
    run_id: str,
    cursor: dict[str, int],
    fail_counter: dict[str, int],
) -> tuple[int, str]:
    """Enumerate new orgs from ProPublica. Returns (found_count, exit_reason)."""
    seen: set[str] = {row[0] for row in conn.execute("SELECT ein FROM nonprofits_seed")}
    found = 0
    exit_reason = "exhausted"

    for state in states:
        if found >= target:
            exit_reason = "target_met"
            break
        for ntee_major in ntee_majors:
            if found >= target:
                exit_reason = "target_met"
                break
            key = f"{state}:{ntee_major}"
            # cursor[key] = last committed page; resume from cursor[key]+1
            start_page = cursor.get(key, -1) + 1
            page = start_page

            while found < target:
                url = f"{PROPUBLICA_SEARCH}?state%5Bid%5D={state}&page={page}"
                try:
                    data = _fetch_with_retry(url, fail_counter=fail_counter)
                except _RateLimited:
                    _finish_run(conn, run_id, found, "rate_limited", 0)
                except _SkipPage:
                    break  # cursor does NOT advance
                except _SkipPair as exc:
                    log.warning("skip_pair state=%s ntee=%s reason=%s", state, ntee_major, exc)
                    break
                except _InfraError:
                    _finish_run(conn, run_id, found, "infra_error", 1)

                orgs = data.get("organizations") or []
                if not orgs:
                    break

                page_added = 0
                for o in orgs:
                    if found >= target:
                        break
                    # Step 6: EIN validation
                    raw_ein = str(o.get("ein", "")).zfill(9)
                    if not _EIN_RE.fullmatch(raw_ein):
                        log.warning("invalid_ein ein=%s; skipped", raw_ein)
                        continue
                    ein = raw_ein
                    if ein in seen:
                        continue
                    # NTEE major filter (client-side)
                    ntee_char = (o.get("ntee_code") or "")[:1]
                    if ntee_char not in ntee_majors:
                        continue
                    # Revenue filter via per-org endpoint
                    try:
                        detail = _fetch_org_revenue(ein, fail_counter=fail_counter)
                    except _RateLimited:
                        _finish_run(conn, run_id, found, "rate_limited", 0)
                    except _InfraError:
                        _finish_run(conn, run_id, found, "infra_error", 1)
                    time.sleep(SLEEP_BETWEEN_CALLS)
                    if detail is None or detail.revenue is None \
                            or detail.revenue < rev_min or detail.revenue > rev_max:
                        continue
                    # Step 6: input validation / truncation
                    name: str = (o.get("name") or "")[:200]
                    city: str | None = (o.get("city") or None)
                    if city:
                        city = city[:200]
                    address: str | None = detail.address
                    if address:
                        address = address[:200]
                    zipcode: str | None = detail.zipcode
                    if zipcode:
                        zipcode = zipcode[:20]
                    ntee_code: str | None = (detail.ntee_code or o.get("ntee_code") or None)
                    if ntee_code:
                        ntee_code = ntee_code[:6]
                    conn.execute(
                        "INSERT OR IGNORE INTO nonprofits_seed"
                        " (ein, name, address, city, state, zipcode, ntee_code, revenue,"
                        "  subsection_code, activity_codes, classification_codes,"
                        "  foundation_code, ruling_date, accounting_period,"
                        "  website_url, website_candidates_json, discovered_at, run_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                        (
                            ein,
                            name,
                            address,
                            city,
                            o.get("state") or state,
                            zipcode,
                            ntee_code,
                            detail.revenue,
                            detail.subsection_code,
                            detail.activity_codes,
                            detail.classification_codes,
                            detail.foundation_code,
                            detail.ruling_date,
                            detail.accounting_period,
                            iso_now(),
                            run_id,
                        ),
                    )
                    conn.commit()
                    seen.add(ein)
                    found += 1
                    page_added += 1
                    log.info("org ein=%s name=%s state=%s", ein, name[:40], state)

                # Cursor advances AFTER successful commit
                cursor[key] = page
                conn.execute(
                    "UPDATE runs SET last_page_scanned=? WHERE run_id=?",
                    (json.dumps(cursor), run_id),
                )
                conn.commit()
                log.info(
                    "page state=%s ntee=%s page=%d added=%d",
                    state,
                    ntee_major,
                    page,
                    page_added,
                )

                num_pages = data.get("num_pages", 0)
                if num_pages and page >= num_pages - 1:
                    break
                page += 1
                time.sleep(SLEEP_BETWEEN_CALLS)

    return found, exit_reason


# ── Step 2: CLI ───────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="seed_enumerate",
        description="Enumerate nonprofits from ProPublica into seeds.db",
    )
    ap.add_argument(
        "--states",
        default=",".join(DEFAULT_STATES),
        help="Comma-separated 2-letter state codes (default: CA,NY,MA,WA,OR,CT,NJ,MD,RI)",
    )
    ap.add_argument(
        "--ntee-majors",
        dest="ntee_majors",
        default=",".join(DEFAULT_NTEE_MAJORS),
        help="Comma-separated single-letter NTEE major codes (default: A,B,E,P)",
    )
    ap.add_argument(
        "--revenue-min",
        dest="revenue_min",
        type=int,
        default=DEFAULT_REV_MIN,
        help="Minimum totrevenue from most recent 990 (default: 1_000_000)",
    )
    ap.add_argument(
        "--revenue-max",
        dest="revenue_max",
        type=int,
        default=DEFAULT_REV_MAX,
        help="Maximum totrevenue (default: 30_000_000)",
    )
    ap.add_argument(
        "--target",
        type=int,
        default=DEFAULT_TARGET,
        help="Stop after N new orgs added (default: 100)",
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "seeds.db",
        help="Path to seeds.db",
    )
    return ap


def parse_and_validate(argv: list[str] | None = None) -> argparse.Namespace:
    ap = build_parser()
    args = ap.parse_args(argv)

    states = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    for s in states:
        if len(s) != 2 or not s.isalpha():
            ap.error(f"Invalid state code: {s!r} — must be 2 uppercase letters")
    args.states_list = states

    ntee_majors = [c.strip().upper() for c in args.ntee_majors.split(",") if c.strip()]
    for c in ntee_majors:
        if len(c) != 1 or not c.isalpha():
            ap.error(f"Invalid NTEE major: {c!r} — must be a single uppercase letter")
    args.ntee_majors_list = ntee_majors

    if args.revenue_min >= args.revenue_max:
        ap.error(
            f"--revenue-min ({args.revenue_min}) must be less than "
            f"--revenue-max ({args.revenue_max})"
        )
    return args


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    args = parse_and_validate(argv)
    conn = ensure_db(args.db)
    _check_filter_consistency(
        conn, args.states_list, args.ntee_majors_list, args.revenue_min, args.revenue_max
    )
    run_id, cursor = _get_or_create_run(
        conn, args.states_list, args.ntee_majors_list, args.revenue_min, args.revenue_max
    )
    fail_counter: dict[str, int] = {"count": 0}
    added, exit_reason = enumerate_new_orgs(
        conn,
        target=args.target,
        states=args.states_list,
        ntee_majors=args.ntee_majors_list,
        rev_min=args.revenue_min,
        rev_max=args.revenue_max,
        run_id=run_id,
        cursor=cursor,
        fail_counter=fail_counter,
    )
    _finish_run(conn, run_id, added, exit_reason, 0)


if __name__ == "__main__":
    main()
