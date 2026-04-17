"""AC1: Schema CHECK constraints reject bad rows."""
import sqlite3

import pytest

from lavandula.nonprofits.schema import ensure_db


@pytest.fixture
def conn(tmp_path):
    return ensure_db(tmp_path / "x.db")


def _minimal_row(**overrides):
    base = dict(
        ein="123456789",
        name="Example",
        cn_profile_url="https://www.charitynavigator.org/ein/123456789",
        last_fetched_at="2026-04-17T00:00:00",
        content_sha256="x" * 64,
    )
    base.update(overrides)
    cols = ",".join(base.keys())
    placeholders = ",".join(f":{k}" for k in base.keys())
    return f"INSERT INTO nonprofits ({cols}) VALUES ({placeholders})", base


def test_ein_length_check(conn):
    sql, vals = _minimal_row(ein="12345")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, vals)


def test_rating_stars_range(conn):
    sql, vals = _minimal_row(rating_stars=5)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, vals)


def test_overall_score_range(conn):
    sql, vals = _minimal_row(overall_score=120.0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, vals)


def test_parse_status_enum(conn):
    sql, vals = _minimal_row(parse_status="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, vals)


def test_website_url_reason_enum(conn):
    sql, vals = _minimal_row(website_url_reason="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, vals)


def test_valid_row(conn):
    sql, vals = _minimal_row()
    conn.execute(sql, vals)
    row = conn.execute("SELECT ein, parse_status FROM nonprofits").fetchone()
    assert row[0] == "123456789"
    assert row[1] == "ok"


def test_fetch_log_status_enum(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fetch_log (url, attempt, fetch_status, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("x", 1, "bogus_status", "2026-04-17"),
        )
