"""Unit tests for TICK-008: Capture IRS fields from ProPublica per-org endpoint.

All tests are fully mocked — no network calls.
Run with: pytest lavandula/nonprofits/tests/unit/test_seed_enumerate_008.py
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import lavandula.nonprofits.tools.seed_enumerate as se


# ── helpers ───────────────────────────────────────────────────────────────────

def _in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(se.SCHEMA_SQL)
    se._apply_migrations(conn)
    return conn


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(nonprofits_seed)")}


def _propublica_org_response(
    *,
    totrevenue: int = 5_000_000,
    ntee_code: str = "A20",
    address: str = "431 18th St NW",
    zipcode: str = "20006-3008",
    subsection_code: int = 3,
    activity_codes: str = "041000000",
    classification_codes: str = "1000",
    foundation_code: int = 15,
    ruling_date: str = "2015-04-01",
    accounting_period: int = 6,
) -> dict:
    return {
        "organization": {
            "ntee_code": ntee_code,
            "address": address,
            "zipcode": zipcode,
            "subsection_code": subsection_code,
            "activity_codes": activity_codes,
            "classification_codes": classification_codes,
            "foundation_code": foundation_code,
            "ruling_date": ruling_date,
            "accounting_period": accounting_period,
        },
        "filings_with_data": [{"totrevenue": totrevenue}],
    }


def _search_page(eins: list[str], ntee: str = "A", state: str = "MA") -> dict:
    orgs = [
        {"ein": int(e), "name": f"Org {e}", "city": "Boston", "state": state, "ntee_code": ntee}
        for e in eins
    ]
    return {"organizations": orgs, "num_pages": 1}


# ── AC1: six new columns exist after ensure_db on a fresh DB ──────────────────

def test_new_columns_exist():
    conn = _in_memory_db()
    cols = _columns(conn)
    for col in (
        "address",
        "zipcode",
        "subsection_code",
        "activity_codes",
        "classification_codes",
        "foundation_code",
        "ruling_date",
        "accounting_period",
    ):
        assert col in cols, f"Missing column: {col}"


# ── AC2: _apply_migrations is idempotent (no OperationalError on second call) ─

def test_migrations_idempotent():
    conn = _in_memory_db()
    # First call already happened in _in_memory_db; call again
    se._apply_migrations(conn)  # must not raise


# ── AC3a: _fetch_org_revenue returns OrgDetail with correct fields ─────────────

def test_fetch_org_revenue_returns_orgdetail():
    resp = _propublica_org_response(accounting_period=6)
    with patch("urllib.request.urlopen") as mock_open:
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = lambda n=-1: json.dumps(resp).encode()
        mock_open.return_value = cm

        detail = se._fetch_org_revenue("123456789", fail_counter={"count": 0})

    assert isinstance(detail, se.OrgDetail)
    assert detail.revenue == 5_000_000
    assert detail.ntee_code == "A20"
    assert detail.address == "431 18th St NW"
    assert detail.zipcode == "20006-3008"
    assert detail.subsection_code == 3
    assert detail.activity_codes == "041000000"
    assert detail.classification_codes == "1000"
    assert detail.foundation_code == 15
    assert detail.ruling_date == "2015-04-01"
    assert detail.accounting_period == 6


# ── AC3b: full enumeration path stores accounting_period in DB ────────────────

def test_accounting_period_stored():
    conn = _in_memory_db()
    search = _search_page(["123456789"], ntee="A", state="MA")
    org_detail = _propublica_org_response(accounting_period=6, totrevenue=5_000_000)

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=30):
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        call_count["n"] += 1
        if call_count["n"] == 1:
            cm.read = lambda n=-1: json.dumps(search).encode()
        else:
            cm.read = lambda n=-1: json.dumps(org_detail).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("time.sleep"):
        run_id = "testrun001"
        conn.execute(
            "INSERT INTO runs(run_id, started_at, filters_json, found_count) VALUES (?,?,?,0)",
            (run_id, se.iso_now(), json.dumps({})),
        )
        conn.commit()
        se.enumerate_new_orgs(
            conn,
            target=1,
            states=["MA"],
            ntee_majors=["A"],
            rev_min=1_000_000,
            rev_max=30_000_000,
            run_id=run_id,
            cursor={},
            fail_counter={"count": 0},
        )

    row = conn.execute(
        "SELECT accounting_period FROM nonprofits_seed WHERE ein='123456789'"
    ).fetchone()
    assert row is not None, "Row not inserted"
    assert row[0] == 6


def test_address_and_zipcode_stored():
    conn = _in_memory_db()
    search = _search_page(["123456789"], ntee="A", state="MA")
    org_detail = _propublica_org_response(
        address="100 Main St Ste 200",
        zipcode="02108-1234",
        totrevenue=5_000_000,
    )

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=30):
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        call_count["n"] += 1
        if call_count["n"] == 1:
            cm.read = lambda n=-1: json.dumps(search).encode()
        else:
            cm.read = lambda n=-1: json.dumps(org_detail).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("time.sleep"):
        run_id = "testrun003"
        conn.execute(
            "INSERT INTO runs(run_id, started_at, filters_json, found_count) VALUES (?,?,?,0)",
            (run_id, se.iso_now(), json.dumps({})),
        )
        conn.commit()
        se.enumerate_new_orgs(
            conn,
            target=1,
            states=["MA"],
            ntee_majors=["A"],
            rev_min=1_000_000,
            rev_max=30_000_000,
            run_id=run_id,
            cursor={},
            fail_counter={"count": 0},
        )

    row = conn.execute(
        "SELECT address, zipcode FROM nonprofits_seed WHERE ein='123456789'"
    ).fetchone()
    assert row is not None
    assert row[0] == "100 Main St Ste 200"
    assert row[1] == "02108-1234"


# ── AC4: None return from _fetch_org_revenue → row skipped, no crash ──────────

def test_none_return_no_crash():
    conn = _in_memory_db()
    search = _search_page(["123456789"], ntee="A", state="MA")

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=30):
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        call_count["n"] += 1
        if call_count["n"] == 1:
            cm.read = lambda n=-1: json.dumps(search).encode()
        else:
            # Simulate org endpoint returning empty filings → detail.revenue=None
            cm.read = lambda n=-1: json.dumps({"organization": {}, "filings_with_data": []}).encode()
        return cm

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("time.sleep"):
        run_id = "testrun002"
        conn.execute(
            "INSERT INTO runs(run_id, started_at, filters_json, found_count) VALUES (?,?,?,0)",
            (run_id, se.iso_now(), json.dumps({})),
        )
        conn.commit()
        # Patch _fetch_org_revenue to return None directly (simulates HTTP failure)
        with patch.object(se, "_fetch_org_revenue", return_value=None):
            found, reason = se.enumerate_new_orgs(
                conn,
                target=5,
                states=["MA"],
                ntee_majors=["A"],
                rev_min=1_000_000,
                rev_max=30_000_000,
                run_id=run_id,
                cursor={},
                fail_counter={"count": 0},
            )

    count = conn.execute("SELECT COUNT(*) FROM nonprofits_seed").fetchone()[0]
    assert count == 0, "Row must not be inserted when _fetch_org_revenue returns None"


# ── AC6: malformed/blank upstream values stored as NULL ───────────────────────

def test_malformed_fields():
    malformed = {
        "organization": {
            "ntee_code": "A20",
            "subsection_code": "",       # blank → None
            "activity_codes": None,      # None → None
            "classification_codes": "1000",
            "foundation_code": "bad",    # non-numeric → None
            "ruling_date": "2020-01-01",
            "accounting_period": 12,
        },
        "filings_with_data": [{"totrevenue": 5_000_000}],
    }

    with patch("urllib.request.urlopen") as mock_open:
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = lambda n=-1: json.dumps(malformed).encode()
        mock_open.return_value = cm

        detail = se._fetch_org_revenue("987654321", fail_counter={"count": 0})

    assert detail is not None
    assert detail.address is None
    assert detail.zipcode is None
    assert detail.subsection_code is None
    assert detail.activity_codes is None
    assert detail.foundation_code is None
    # Valid fields still populated
    assert detail.revenue == 5_000_000
    assert detail.accounting_period == 12
