"""AC14b, AC20: DB upsert, SQL injection round-trip."""
from lavandula.nonprofits import db_writer
from lavandula.nonprofits.extract import ExtractedProfile
from lavandula.nonprofits.schema import ensure_db


def _profile(**kw):
    base = dict(
        ein="530196605",
        name="American Red Cross",
        mission="Prevent human suffering.",
        website_url="https://www.redcross.org",
        website_url_raw="https://www.redcross.org",
        website_url_reason=None,
        rating_stars=4,
        overall_score=91.5,
        beacons_completed=4,
        rated=1,
        total_revenue=3_456_789_012,
        total_expenses=3_201_000_000,
        program_expense_pct=89.5,
        ntee_major="M",
        ntee_code="M20",
        state="DC",
        parse_status="ok",
    )
    base.update(kw)
    return ExtractedProfile(**base)


def test_upsert(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    db_writer.upsert_nonprofit(
        conn, _profile(),
        cn_profile_url="https://www.charitynavigator.org/ein/530196605",
        content_sha256="deadbeef",
    )
    row = conn.execute("SELECT name, state FROM nonprofits").fetchone()
    assert row[0] == "American Red Cross"
    assert row[1] == "DC"


def test_sql_injection_mission_roundtrip(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    payload = "'; DROP TABLE nonprofits; --"
    p = _profile(mission=payload)
    db_writer.upsert_nonprofit(
        conn, p,
        cn_profile_url="https://www.charitynavigator.org/ein/530196605",
        content_sha256="x",
    )
    row = conn.execute("SELECT mission FROM nonprofits").fetchone()
    assert row[0] == payload
    # Table still exists.
    conn.execute("SELECT COUNT(*) FROM nonprofits").fetchone()


def test_redirected_dedup_query(tmp_path):
    """AC14b: GROUP BY COALESCE(redirected_to_ein, ein) collapses pair."""
    conn = ensure_db(tmp_path / "x.db")
    # Row A (redirected to B)
    a = _profile(ein="111111111", name="Src Org")
    db_writer.upsert_nonprofit(
        conn, a,
        cn_profile_url="https://www.charitynavigator.org/ein/111111111",
        content_sha256="x",
        redirected_to_ein="222222222",
    )
    # Row B (independent)
    b = _profile(ein="222222222", name="Target Org")
    db_writer.upsert_nonprofit(
        conn, b,
        cn_profile_url="https://www.charitynavigator.org/ein/222222222",
        content_sha256="y",
    )
    rows = conn.execute(
        "SELECT COALESCE(redirected_to_ein, ein) AS canon FROM nonprofits"
    ).fetchall()
    canonicals = {r[0] for r in rows}
    assert canonicals == {"222222222"}


def test_fetch_log_insert(tmp_path):
    conn = ensure_db(tmp_path / "x.db")
    db_writer.insert_fetch_log(
        conn, ein="530196605", url="https://x", status_code=200,
        attempt=1, is_retry=False, fetch_status="ok",
        elapsed_ms=100, bytes_read=1000,
    )
    n = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    assert n == 1


def test_log_crlf_sanitized_in_db(tmp_path):
    """AC21: remote strings with CR/LF must be stripped before DB insert."""
    conn = ensure_db(tmp_path / "x.db")
    db_writer.insert_fetch_log(
        conn, ein=None, url="https://x", status_code=429,
        attempt=1, is_retry=False, fetch_status="rate_limited",
        elapsed_ms=1, bytes_read=0,
        notes="Retry-After: 60\r\nFAKE_LOG_LINE",
    )
    row = conn.execute("SELECT notes FROM fetch_log").fetchone()
    # \r\n must be stripped.
    assert "\r" not in row[0]
    assert "\n" not in row[0]
