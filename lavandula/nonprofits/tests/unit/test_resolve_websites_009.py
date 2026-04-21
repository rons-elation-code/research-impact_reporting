from __future__ import annotations

import sqlite3

from lavandula.nonprofits.tools.resolve_websites import resolve_batch


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


def test_resolver_rejects_directory_domains():
    conn = _make_db()
    _insert(conn, "100000001", "Hospice Of Wichita Falls Inc", "Wichita Falls")

    def mock_fn(query, *, key):
        return {
            "web": {
                "results": [
                    {"url": "https://greatnonprofits.org"},
                    {"url": "https://theorg.com"},
                    {"url": "https://govtribe.com"},
                ]
            }
        }

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=__import__("logging").getLogger("t"), _search_fn=mock_fn)

    row = conn.execute(
        "SELECT website_url, resolver_status, resolver_reason FROM nonprofits_seed WHERE ein='100000001'"
    ).fetchone()
    assert row == (None, "rejected", "no-non-blocklist-result")


def test_resolver_marks_ambiguous_and_does_not_write_website():
    conn = _make_db()
    _insert(conn, "100000002", "Example Community Health", "Austin")

    def mock_fn(query, *, key):
        return {
            "web": {
                "results": [
                    {
                        "url": "https://examplehealth.org",
                        "title": "Example Community Health",
                        "description": "Austin nonprofit official website",
                    },
                    {
                        "url": "https://examplecommunityhealth.org",
                        "title": "Example Community Health",
                        "description": "Austin nonprofit official website",
                    },
                ]
            }
        }

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=__import__("logging").getLogger("t"), _search_fn=mock_fn)

    row = conn.execute(
        "SELECT website_url, resolver_status, resolver_confidence FROM nonprofits_seed WHERE ein='100000002'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "ambiguous"
    assert row[2] is not None


def test_resolver_stores_metadata_for_accepted_result():
    conn = _make_db()
    _insert(conn, "100000003", "Genesys Works", "Houston")

    def mock_fn(query, *, key):
        return {
            "web": {
                "results": [
                    {
                        "url": "https://genesysworks.org",
                        "title": "Genesys Works",
                        "description": "Official nonprofit website helping students",
                    },
                    {
                        "url": "https://linkedin.com/company/genesys-works",
                        "title": "Genesys Works LinkedIn",
                        "description": "Profile",
                    },
                ]
            }
        }

    resolve_batch(conn, key="k", limit=0, min_sleep=0, dry_run=False, log=__import__("logging").getLogger("t"), _search_fn=mock_fn)

    row = conn.execute(
        "SELECT website_url, resolver_status, resolver_method, resolver_confidence FROM nonprofits_seed WHERE ein='100000003'"
    ).fetchone()
    assert row[0] == "https://genesysworks.org"
    assert row[1] == "accepted"
    assert row[2] == "brave-scored"
    assert row[3] >= 0.85
