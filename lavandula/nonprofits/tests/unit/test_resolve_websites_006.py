"""TICK-006: Unit tests for lavandula.nonprofits.tools.resolve_websites.

All 22 AC tests + 1 live smoke test (pytest -m live).
Network I/O is avoided via dependency injection or monkeypatching.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from lavandula.nonprofits.tools.resolve_websites import (
    BLOCKLIST_HOSTS,
    _is_blocklisted,
    _pick_primary,
    _search_with_retry,
    _validate_url,
    resolve_batch,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

_SEED_SCHEMA = """
CREATE TABLE IF NOT EXISTS nonprofits_seed (
  ein                     TEXT PRIMARY KEY,
  name                    TEXT,
  city                    TEXT,
  state                   TEXT,
  ntee_code               TEXT,
  revenue                 INTEGER,
  website_url             TEXT,
  website_candidates_json TEXT,
  discovered_at           TEXT,
  run_id                  TEXT,
  notes                   TEXT
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SEED_SCHEMA)
    return conn


def _insert(conn, ein, name, city=None, website_url=None):
    conn.execute(
        "INSERT INTO nonprofits_seed (ein, name, city, website_url) VALUES (?,?,?,?)",
        (ein, name, city, website_url),
    )
    conn.commit()


def _fake_response(*urls: str) -> dict:
    """Build a minimal Brave response with the given URLs as results."""
    return {"web": {"results": [{"url": u} for u in urls]}}


_LOG = logging.getLogger("test")

# ── 1. test_blocklist_linkedin ────────────────────────────────────────────────

def test_blocklist_linkedin():
    assert _is_blocklisted("linkedin.com") is True
    assert _is_blocklisted("www.linkedin.com") is True


# ── 2. test_blocklist_subdomain ───────────────────────────────────────────────

def test_blocklist_subdomain():
    assert _is_blocklisted("blog.wikipedia.org") is True
    assert _is_blocklisted("en.wikipedia.org") is True


# ── 3. test_blocklist_gov ─────────────────────────────────────────────────────

def test_blocklist_gov():
    assert _is_blocklisted("irs.gov") is True
    assert _is_blocklisted("subdomain.army.mil") is True


# ── 4. test_all_blocklisted_writes_null ───────────────────────────────────────

def test_all_blocklisted_writes_null():
    conn = _make_db()
    _insert(conn, "111111111", "Org A", "Boston")

    blocklisted_urls = [
        "https://linkedin.com/company/org-a",
        "https://facebook.com/orga",
        "https://guidestar.org/profile/111111111",
    ]
    mock_fn = lambda q, *, key: _fake_response(*blocklisted_urls)

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=_LOG, _search_fn=mock_fn)

    row = conn.execute("SELECT website_url, notes FROM nonprofits_seed WHERE ein='111111111'").fetchone()
    assert row[0] is None
    assert row[1] == "no-non-blocklist-result"


# ── 5. test_picks_first_valid ─────────────────────────────────────────────────

def test_picks_first_valid():
    result = _pick_primary([
        {"url": "https://linkedin.com/company/x"},
        {"url": "https://example.org/about/team"},
        {"url": "https://twitter.com/x"},
    ])
    assert result == "https://example.org"


# ── 6. test_url_validation_scheme ─────────────────────────────────────────────

def test_url_validation_scheme():
    assert _validate_url("ftp://example.org") is None
    assert _validate_url("javascript:alert(1)") is None
    assert _validate_url("https://example.org") == "https://example.org"


# ── 7. test_url_validation_punycode ───────────────────────────────────────────

def test_url_validation_punycode():
    assert _validate_url("https://xn--nxasmq6b.com") is None
    assert _validate_url("https://sub.xn--nxasmq6b.com") is None


# ── 8. test_url_validation_no_dot ─────────────────────────────────────────────

def test_url_validation_no_dot():
    assert _validate_url("https://localhost") is None
    assert _validate_url("http://localhost/path") is None


# ── 9. test_url_validation_userinfo ───────────────────────────────────────────

def test_url_validation_userinfo():
    assert _validate_url("https://user@host.com") is None
    assert _validate_url("https://user:pass@host.com") is None


# ── 10. test_url_canonical_form ───────────────────────────────────────────────

def test_url_canonical_form():
    assert _validate_url("https://example.org/about/team?x=1#frag") == "https://example.org"
    assert _validate_url("http://example.com/deep/path") == "http://example.com"


# ── 11. test_idempotent_skip ──────────────────────────────────────────────────

def test_idempotent_skip():
    conn = _make_db()
    _insert(conn, "222222222", "Org B", "NYC", website_url="https://orgb.org")

    calls = []
    mock_fn = lambda q, *, key: (calls.append(q), _fake_response("https://example.org"))[-1]

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=_LOG, _search_fn=mock_fn)

    assert calls == [], "Should not query Brave for rows that already have website_url"


# ── 12. test_retry_on_500 ─────────────────────────────────────────────────────

def test_retry_on_500():
    attempt = [0]

    def mock_search(query, *, key):
        attempt[0] += 1
        if attempt[0] == 1:
            resp = MagicMock()
            resp.status_code = 500
            raise requests.HTTPError(response=resp)
        return _fake_response("https://success.org/page")

    with patch("lavandula.nonprofits.tools.resolve_websites.time.sleep"):
        response, note = _search_with_retry("q", key="k", log=_LOG, brave_search_fn=mock_search)

    assert response is not None
    assert note is None
    results = response["web"]["results"]
    assert results[0]["url"] == "https://success.org/page"


# ── 13. test_retry_exhausted ──────────────────────────────────────────────────

def test_retry_exhausted():
    def always_fail(query, *, key):
        resp = MagicMock()
        resp.status_code = 429
        raise requests.HTTPError(response=resp)

    with patch("lavandula.nonprofits.tools.resolve_websites.time.sleep"):
        response, note = _search_with_retry("q", key="k", log=_LOG, brave_search_fn=always_fail)

    assert response is None
    assert note == "brave_error:429"


# ── 14. test_qps_sleep ────────────────────────────────────────────────────────

def test_qps_sleep():
    conn = _make_db()
    _insert(conn, "333333333", "Org C", "Seattle")

    mock_fn = lambda q, *, key: _fake_response("https://orgc.org")

    with patch("lavandula.nonprofits.tools.resolve_websites.time.sleep") as mock_sleep, \
         patch("lavandula.nonprofits.tools.resolve_websites.time.monotonic", side_effect=[0.0, 0.0]):
        resolve_batch(conn, key="k", limit=0, min_sleep=1.0, dry_run=True, log=_LOG, _search_fn=mock_fn)

    mock_sleep.assert_called_once_with(1.0)


# ── 15. test_dry_run ──────────────────────────────────────────────────────────

def test_dry_run(capsys):
    conn = _make_db()
    _insert(conn, "444444444", "Org D", "Denver")

    mock_fn = lambda q, *, key: _fake_response("https://orgd.org/page")

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=True, log=_LOG, _search_fn=mock_fn)

    row = conn.execute("SELECT website_url FROM nonprofits_seed WHERE ein='444444444'").fetchone()
    assert row[0] is None, "dry_run must not write to DB"

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "https://orgd.org" in out


# ── 16. test_api_key_startup_failure ─────────────────────────────────────────

def test_api_key_startup_failure(tmp_path):
    from lavandula.common.secrets import SecretUnavailable
    from lavandula.nonprofits.tools.resolve_websites import main

    db = tmp_path / "seeds.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SEED_SCHEMA)
    conn.close()

    with patch(
        "lavandula.nonprofits.tools.resolve_websites.get_brave_api_key",
        side_effect=SecretUnavailable("no key"),
    ), pytest.raises(SystemExit) as exc_info:
        main(["--db", str(db)])

    assert exc_info.value.code == 1


# ── 17. test_response_body_cap ────────────────────────────────────────────────

def test_response_body_cap():
    conn = _make_db()
    _insert(conn, "555555555", "Org E", "Chicago")

    many_results = [{"url": f"https://example{i}.org", "description": "x" * 200} for i in range(50)]

    mock_fn = lambda q, *, key: {"web": {"results": many_results}}

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=_LOG, _search_fn=mock_fn)

    row = conn.execute("SELECT website_candidates_json FROM nonprofits_seed WHERE ein='555555555'").fetchone()
    stored = row[0]
    assert stored is not None
    assert len(stored) <= 8192, f"candidates_json too large: {len(stored)} bytes"
    parsed = json.loads(stored)  # must be valid JSON
    assert len(parsed["web"]["results"]) == 3


# ── 18. test_limit_flag ───────────────────────────────────────────────────────

def test_limit_flag():
    conn = _make_db()
    for i in range(5):
        _insert(conn, f"60000000{i}", f"Org {i}", "Miami")

    processed = []
    def mock_fn(q, *, key):
        processed.append(q)
        return _fake_response("https://example.org")

    resolve_batch(conn, key="k", limit=2, min_sleep=0, dry_run=False, log=_LOG, _search_fn=mock_fn)

    assert len(processed) == 2


# ── 19. test_cli_invalid_qps ─────────────────────────────────────────────────

def test_cli_invalid_qps(tmp_path, capsys):
    from lavandula.nonprofits.tools.resolve_websites import main
    from lavandula.common.secrets import SecretUnavailable

    db = tmp_path / "seeds.db"

    with pytest.raises(SystemExit) as exc_info:
        main(["--db", str(db), "--qps", "0"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        main(["--db", str(db), "--qps", "-1"])
    assert exc_info.value.code == 2


# ── 20. test_cli_invalid_limit ───────────────────────────────────────────────

def test_cli_invalid_limit(tmp_path):
    from lavandula.nonprofits.tools.resolve_websites import main

    db = tmp_path / "seeds.db"

    with pytest.raises(SystemExit) as exc_info:
        main(["--db", str(db), "--limit", "-1"])
    assert exc_info.value.code == 2


# ── 21. test_url_validation_ip_rejected ──────────────────────────────────────

def test_url_validation_ip_rejected():
    assert _validate_url("http://127.0.0.1/path") is None
    assert _validate_url("http://169.254.169.254") is None
    assert _validate_url("https://[::1]/") is None


# ── 22. test_query_quote_sanitization ────────────────────────────────────────

def test_query_quote_sanitization():
    conn = _make_db()
    _insert(conn, "777777777", 'The "Help" Foundation', "San Francisco")

    captured_queries = []

    def mock_fn(query, *, key):
        captured_queries.append(query)
        return _fake_response("https://helpfoundation.org")

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=_LOG, _search_fn=mock_fn)

    assert len(captured_queries) == 1
    q = captured_queries[0]
    # The name's inner double quotes must be stripped; outer quotes wrap a clean name
    assert '"The Help Foundation"' in q, f"Expected clean quoted name in: {q!r}"
    # No consecutive double-quotes from unescaped inner quotes
    assert '""' not in q


# ── Live smoke test (pytest -m live) ─────────────────────────────────────────

@pytest.mark.live
def test_live_smoke_3_orgs(tmp_path):
    """3 well-known orgs: ILRC, Self-Help for the Elderly, New Roads School.
    Asserts: website_url non-empty, host not blocklisted, scheme is https. (AC10)
    """
    from lavandula.common.secrets import SecretUnavailable, get_brave_api_key

    try:
        key = get_brave_api_key()
    except SecretUnavailable:
        pytest.skip("Brave API key not available")

    orgs = [
        ("260832353", "Immigrant Legal Resource Center", "San Francisco"),
        ("941575939", "Self-Help for the Elderly", "San Francisco"),
        ("954271352", "New Roads School", "Santa Monica"),
    ]

    conn = sqlite3.connect(":memory:")
    conn.executescript(_SEED_SCHEMA)
    for ein, name, city in orgs:
        _insert(conn, ein, name, city)

    resolve_batch(conn, key=key, limit=0, min_sleep=1.0, dry_run=False, log=_LOG)

    for ein, name, _ in orgs:
        row = conn.execute(
            "SELECT website_url FROM nonprofits_seed WHERE ein=?", (ein,)
        ).fetchone()
        url = row[0]
        assert url is not None, f"No URL found for {name}"
        assert url.startswith("https://"), f"Expected https URL for {name}: {url}"
        host = url.split("://", 1)[1].split("/")[0]
        assert not _is_blocklisted(host), f"Blocklisted host for {name}: {host}"
