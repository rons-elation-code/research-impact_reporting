"""Unit tests for TICK-005: Productionize ProPublica seed enumerator.

All tests are fully mocked — no network calls.
Run with: pytest lavandula/nonprofits/tests/unit/test_seed_enumerate_005.py
Live smoke test: pytest -m live lavandula/nonprofits/tests/unit/test_seed_enumerate_005.py
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import lavandula.nonprofits.tools.seed_enumerate as se


# ── helpers ───────────────────────────────────────────────────────────────────

def _in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(se.SCHEMA_SQL)
    se._apply_migrations(conn)
    return conn


def _mock_response(body: bytes) -> MagicMock:
    """Context-manager response whose read(n) returns body[:n]."""
    m = MagicMock()
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    m.read = lambda n=-1: body if n < 0 else body[:n]
    return m


def _json_response(data: dict) -> MagicMock:
    return _mock_response(json.dumps(data).encode())


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = MagicMock()
    hdrs.get = lambda key, default="": (
        retry_after if (key == "Retry-After" and retry_after is not None) else default
    )
    return urllib.error.HTTPError(url="", code=code, msg=f"HTTP {code}", hdrs=hdrs, fp=None)


def _search_page(eins: list[str], num_pages: int = 1, ntee: str = "A", state: str = "MA") -> dict:
    return {
        "organizations": [
            {"ein": e, "name": f"Org {e}", "city": "Boston", "state": state, "ntee_code": ntee}
            for e in eins
        ],
        "num_pages": num_pages,
    }


def _org_detail(ein: str, revenue: int = 5_000_000, ntee: str = "A01") -> dict:
    return {
        "organization": {"ein": ein, "ntee_code": ntee},
        "filings_with_data": [{"totrevenue": revenue}],
    }


# ── Step 2: CLI tests (AC1, AC2) ──────────────────────────────────────────────

def test_cli_defaults():
    """Parsing no args should produce all expected defaults."""
    args = se.parse_and_validate([])
    assert args.states_list == list(se.DEFAULT_STATES)
    assert args.ntee_majors_list == list(se.DEFAULT_NTEE_MAJORS)
    assert args.revenue_min == se.DEFAULT_REV_MIN
    assert args.revenue_max == se.DEFAULT_REV_MAX
    assert args.target == se.DEFAULT_TARGET


def test_cli_states_flag():
    """--states TX,OK should parse to ["TX", "OK"]."""
    args = se.parse_and_validate(["--states", "TX,OK"])
    assert args.states_list == ["TX", "OK"]


def test_cli_invalid_state():
    """--states TEXAS should exit 2 (state code must be 2 letters)."""
    with pytest.raises(SystemExit) as exc:
        se.parse_and_validate(["--states", "TEXAS"])
    assert exc.value.code == 2


def test_cli_invalid_ntee():
    """--ntee-majors AB should exit 2 (must be single letter)."""
    with pytest.raises(SystemExit) as exc:
        se.parse_and_validate(["--ntee-majors", "AB"])
    assert exc.value.code == 2


def test_cli_revenue_min_gte_max():
    """--revenue-min >= --revenue-max should exit 2."""
    with pytest.raises(SystemExit) as exc:
        se.parse_and_validate(["--revenue-min", "5000000", "--revenue-max", "1000000"])
    assert exc.value.code == 2


# ── revenue filter (AC5) ──────────────────────────────────────────────────────

def test_revenue_filter(tmp_path):
    """Org with revenue below --revenue-min must not be inserted."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    search_body = _search_page(["123456789"])
    org_body = _org_detail("123456789", revenue=500_000)  # below min

    responses = iter([_json_response(search_body), _json_response(org_body)])

    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: next(responses)):
        with patch("time.sleep"):
            found, _ = se.enumerate_new_orgs(
                conn,
                target=1,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    assert found == 0
    assert conn.execute("SELECT COUNT(*) FROM nonprofits_seed").fetchone()[0] == 0


# ── NTEE filter (AC4) ─────────────────────────────────────────────────────────

def test_ntee_filter(tmp_path):
    """Org with NTEE major 'Z' not in ntee_majors must be filtered out."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    search_body = _search_page(["123456789"], ntee="Z")  # Z not in ["A"]
    # No org detail call should happen because the NTEE filter rejects it first

    with patch("urllib.request.urlopen", return_value=_json_response(search_body)):
        with patch("time.sleep"):
            found, _ = se.enumerate_new_orgs(
                conn,
                target=1,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    assert found == 0


# ── cursor tests (AC7) ────────────────────────────────────────────────────────

def test_cursor_advances(tmp_path):
    """After 2 successful pages, cursor in runs reflects the last page number."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    page0_search = _search_page(["000000001"], num_pages=2)
    page0_org = _org_detail("000000001", revenue=5_000_000)
    page1_search = _search_page(["000000002"], num_pages=2)
    page1_org = _org_detail("000000002", revenue=5_000_000)
    responses = iter([
        _json_response(page0_search),
        _json_response(page0_org),
        _json_response(page1_search),
        _json_response(page1_org),
    ])

    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: next(responses)):
        with patch("time.sleep"):
            se.enumerate_new_orgs(
                conn,
                target=2,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    row = conn.execute("SELECT last_page_scanned FROM runs WHERE run_id=?", (run_id,)).fetchone()
    stored_cursor = json.loads(row[0])
    assert stored_cursor.get("MA:A") == 1  # page 1 was the last committed page


def test_resume_uses_cursor(tmp_path):
    """When runs has cursor {"TX:A": 2}, enumeration starts at page 3."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    # Insert a partial (incomplete) run with cursor showing TX:A=2
    cursor_json = json.dumps({"TX:A": 2})
    run_id = "resumetest"
    conn.execute(
        "INSERT INTO runs(run_id, started_at, filters_json, found_count, last_page_scanned)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, se.iso_now(), json.dumps({"states": ["TX"], "ntee_majors": ["A"],
                                           "rev_min": 1_000_000, "rev_max": 30_000_000}),
         0, cursor_json),
    )
    conn.commit()

    _, resumed_cursor = se._get_or_create_run(conn, ["TX"], ["A"], 1_000_000, 30_000_000)
    assert resumed_cursor == {"TX:A": 2}

    captured_urls: list[str] = []

    def _capture(req, **kw):
        captured_urls.append(req.full_url)
        return _json_response({"organizations": [], "num_pages": 5})

    with patch("urllib.request.urlopen", side_effect=_capture):
        with patch("time.sleep"):
            se.enumerate_new_orgs(
                conn,
                target=1,
                states=["TX"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=resumed_cursor,
                fail_counter={"count": 0},
            )

    # First URL fetched must have page=3
    assert captured_urls, "expected at least one HTTP call"
    assert "page=3" in captured_urls[0]


# ── filter mismatch guard (AC7) ───────────────────────────────────────────────

def test_filter_mismatch_exits(tmp_path):
    """Existing run with states=['CA'] + new invocation --states TX must exit 2."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    conn.execute(
        "INSERT INTO runs(run_id, started_at, filters_json, found_count)"
        " VALUES (?, ?, ?, 0)",
        ("run1", se.iso_now(),
         json.dumps({"states": ["CA"], "ntee_majors": ["A"], "rev_min": 1_000_000, "rev_max": 30_000_000})),
    )
    conn.commit()

    with pytest.raises(SystemExit) as exc:
        se._check_filter_consistency(conn, ["TX"], ["A"], 1_000_000, 30_000_000)
    assert exc.value.code == 2


# ── HTTP retry tests (AC8) ────────────────────────────────────────────────────

def test_429_retry_then_exit0(tmp_path):
    """3 consecutive 429 responses → enumerate_new_orgs commits and calls sys.exit(0)."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    with patch("urllib.request.urlopen", side_effect=_http_error(429)):
        with patch("time.sleep"):
            with pytest.raises(SystemExit) as exc:
                se.enumerate_new_orgs(
                    conn,
                    target=10,
                    states=["MA"],
                    ntee_majors=["A"],
                    rev_min=1_000_000,
                    rev_max=30_000_000,
                    run_id=run_id,
                    cursor=cursor,
                    fail_counter=fail_counter,
                )

    assert exc.value.code == 0
    row = conn.execute(
        "SELECT finished_at, exit_reason FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] is not None, "finished_at must be set"
    assert row[1] == "rate_limited"


def test_5xx_skips_pair(tmp_path):
    """2 consecutive 500 responses → (state, ntee) pair skipped; loop continues."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA", "NY"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    ny_search = _search_page(["111111111"], ntee="A", state="NY")
    ny_org = _org_detail("111111111", revenue=5_000_000)
    # MA:A gets 500×2 → skip; NY:A gets good response.
    # Use a list side_effect so mock auto-raises exception instances.
    with patch("urllib.request.urlopen", side_effect=[
        _http_error(500),          # MA:A attempt 1 → HTTPError raised
        _http_error(500),          # MA:A attempt 2 → _SkipPair
        _json_response(ny_search), # NY:A page 0
        _json_response(ny_org),    # NY:A per-org
    ]):
        with patch("time.sleep"):
            found, reason = se.enumerate_new_orgs(
                conn,
                target=1,
                states=["MA", "NY"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    assert found == 1
    assert conn.execute("SELECT state FROM nonprofits_seed").fetchone()[0] == "NY"


def test_json_parse_error_skips_page(tmp_path):
    """Non-JSON response body → page skipped, no crash."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    with patch("urllib.request.urlopen", return_value=_mock_response(b"not json")):
        with patch("time.sleep"):
            found, _ = se.enumerate_new_orgs(
                conn,
                target=1,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    assert found == 0


def test_5_consecutive_failures_exit1(tmp_path):
    """5 consecutive network errors → enumerate_new_orgs calls sys.exit(1)."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(
        conn, ["CA", "NY", "MA"], ["A", "B", "E"], 1_000_000, 30_000_000
    )
    fail_counter = {"count": 0}

    with patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
        with patch("time.sleep"):
            with pytest.raises(SystemExit) as exc:
                se.enumerate_new_orgs(
                    conn,
                    target=100,
                    states=["CA", "NY", "MA"],
                    ntee_majors=["A", "B", "E"],
                    rev_min=1_000_000,
                    rev_max=30_000_000,
                    run_id=run_id,
                    cursor=cursor,
                    fail_counter=fail_counter,
                )

    assert exc.value.code == 1
    row = conn.execute(
        "SELECT exit_reason FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row[0] == "infra_error"


def test_large_response_rejected(tmp_path, caplog):
    """Response body > 1 MB → page skipped, WARNING logged."""
    import logging

    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    oversized = b"x" * (se.MAX_RESPONSE_BYTES + 2)

    with patch("urllib.request.urlopen", return_value=_mock_response(oversized)):
        with patch("time.sleep"):
            with caplog.at_level(logging.WARNING, logger="lavandula.nonprofits.tools.seed_enumerate"):
                found, _ = se.enumerate_new_orgs(
                    conn,
                    target=1,
                    states=["MA"],
                    ntee_majors=["A"],
                    rev_min=1_000_000,
                    rev_max=30_000_000,
                    run_id=run_id,
                    cursor=cursor,
                    fail_counter=fail_counter,
                )

    assert found == 0
    assert any("large_response" in r.message for r in caplog.records)


# ── input validation tests (AC / Step 6) ─────────────────────────────────────

def test_ein_validation(tmp_path):
    """Org with malformed EIN must not be inserted."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    bad_search = {
        "organizations": [
            {"ein": "ABCXYZ", "name": "Bad EIN Org", "city": "Boston",
             "state": "MA", "ntee_code": "A"}
        ],
        "num_pages": 1,
    }

    with patch("urllib.request.urlopen", return_value=_json_response(bad_search)):
        with patch("time.sleep"):
            found, _ = se.enumerate_new_orgs(
                conn,
                target=1,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    assert found == 0
    assert conn.execute("SELECT COUNT(*) FROM nonprofits_seed").fetchone()[0] == 0


def test_name_truncated(tmp_path):
    """Name longer than 200 chars must be stored as first 200 chars."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    run_id, cursor = se._get_or_create_run(conn, ["MA"], ["A"], 1_000_000, 30_000_000)
    fail_counter = {"count": 0}

    long_name = "A" * 300
    search_body = {
        "organizations": [
            {"ein": "123456789", "name": long_name, "city": "Boston",
             "state": "MA", "ntee_code": "A"}
        ],
        "num_pages": 1,
    }
    org_body = _org_detail("123456789", revenue=5_000_000)
    responses = iter([_json_response(search_body), _json_response(org_body)])

    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: next(responses)):
        with patch("time.sleep"):
            found, _ = se.enumerate_new_orgs(
                conn,
                target=1,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor=cursor,
                fail_counter=fail_counter,
            )

    assert found == 1
    stored = conn.execute("SELECT name FROM nonprofits_seed").fetchone()[0]
    assert len(stored) == 200
    assert stored == long_name[:200]


# ── idempotency (AC6) ─────────────────────────────────────────────────────────

def test_idempotent_rerun(tmp_path):
    """Re-running against same DB for an already-inserted EIN adds no duplicates."""
    conn = se.ensure_db(tmp_path / "seeds.db")
    db_path = tmp_path / "seeds.db"

    search_body = _search_page(["123456789"])
    org_body = _org_detail("123456789", revenue=5_000_000)

    def _run():
        conn2 = se.ensure_db(db_path)
        run_id, cursor = se._get_or_create_run(conn2, ["MA"], ["A"], 1_000_000, 30_000_000)
        # Mark the run as finished so the next run starts fresh
        conn2.execute(
            "UPDATE runs SET finished_at=?, exit_reason=? WHERE run_id=?",
            (se.iso_now(), "target_met", run_id),
        )
        conn2.commit()
        fail_counter = {"count": 0}
        responses = iter([_json_response(search_body), _json_response(org_body)])
        with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: next(responses)):
            with patch("time.sleep"):
                se.enumerate_new_orgs(
                    conn2,
                    target=1,
                    states=["MA"],
                    ntee_majors=["A"],
                    rev_min=1_000_000,
                    rev_max=30_000_000,
                    run_id=run_id,
                    cursor=cursor,
                    fail_counter=fail_counter,
                )

    _run()
    _run()

    count = conn.execute("SELECT COUNT(*) FROM nonprofits_seed").fetchone()[0]
    assert count == 1


# ── Step 1: migration idempotency ────────────────────────────────────────────

def test_schema_migrations_idempotent():
    """Calling _apply_migrations twice on the same DB must not raise."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(se.SCHEMA_SQL)
    se._apply_migrations(conn)
    se._apply_migrations(conn)  # second call must not raise
    # Verify new columns exist
    cols_runs = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "last_page_scanned" in cols_runs
    assert "exit_reason" in cols_runs
    cols_seed = {row[1] for row in conn.execute("PRAGMA table_info(nonprofits_seed)")}
    assert "notes" in cols_seed


# ── Step 9: live smoke test ───────────────────────────────────────────────────

@pytest.mark.live
def test_live_smoke_5_orgs(tmp_path):
    """--target 5 --states MA: adds >= 1 MA org. Skips if ProPublica unreachable. (AC9)"""
    import urllib.request

    # Skip if ProPublica is unreachable
    try:
        req = urllib.request.Request(
            "https://projects.propublica.org/nonprofits/api/v2/search.json?state%5Bid%5D=MA&page=0",
            headers={"User-Agent": se.UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(1)
    except Exception:
        pytest.skip("ProPublica API unreachable")

    db_path = tmp_path / "live_seeds.db"
    conn = se.ensure_db(db_path)
    se._check_filter_consistency(conn, ["MA"], list(se.DEFAULT_NTEE_MAJORS),
                                  se.DEFAULT_REV_MIN, se.DEFAULT_REV_MAX)
    run_id, cursor = se._get_or_create_run(
        conn, ["MA"], list(se.DEFAULT_NTEE_MAJORS), se.DEFAULT_REV_MIN, se.DEFAULT_REV_MAX
    )
    fail_counter: dict[str, int] = {"count": 0}
    found, _ = se.enumerate_new_orgs(
        conn,
        target=5,
        states=["MA"],
        ntee_majors=list(se.DEFAULT_NTEE_MAJORS),
        rev_min=se.DEFAULT_REV_MIN,
        rev_max=se.DEFAULT_REV_MAX,
        run_id=run_id,
        cursor=cursor,
        fail_counter=fail_counter,
    )

    assert found >= 1, "expected at least 1 MA org"
    rows = conn.execute(
        "SELECT ein, name, state FROM nonprofits_seed WHERE state=?", ("MA",)
    ).fetchall()
    assert len(rows) >= 1
    for ein, name, state in rows:
        assert len(ein) == 9 and ein.isdigit(), f"invalid EIN: {ein}"
        assert name, "name must be non-empty"
        assert state == "MA"
