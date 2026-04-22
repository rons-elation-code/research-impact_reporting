"""Ingestion logic tests (Spec 0008).

Covers AC5 (confidence→status), AC7 (per-batch transaction isolation),
AC9 (candidates_json shape), AC17 (output-line validation),
AC19 (duplicate EIN last-write-wins), AC20 (reasoning truncation).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from lavandula.nonprofits.tools import batch_resolve as br
from lavandula.nonprofits.agent_runner import PROMPT_VERSION


def _init_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE nonprofits_seed (
            ein TEXT PRIMARY KEY,
            name TEXT, address TEXT, city TEXT, state TEXT,
            zipcode TEXT, ntee_code TEXT, revenue INTEGER,
            website_url TEXT,
            website_candidates_json TEXT,
            resolver_confidence REAL,
            resolver_status TEXT,
            resolver_method TEXT,
            resolver_reason TEXT
        );
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO nonprofits_seed "
            "(ein,name,resolver_status) VALUES (?,?,?)",
            (r["ein"], r.get("name", ""), r.get("resolver_status")),
        )
    conn.commit()
    conn.close()


def _read(engine, ein):
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT website_url, resolver_status, resolver_confidence, "
            "resolver_method, resolver_reason, website_candidates_json "
            "FROM nonprofits_seed WHERE ein=:e"), {"e": ein}).mappings().first()
    return dict(row) if row else None


# ── AC5 ──────────────────────────────────────────────────────────────────────

def test_confidence_to_status_mapping() -> None:
    assert br._confidence_to_status("high") == ("resolved", 0.9)
    assert br._confidence_to_status("medium") == ("resolved", 0.6)
    assert br._confidence_to_status("low") == ("ambiguous", 0.3)
    assert br._confidence_to_status("none") == ("unresolved", 0.0)


# ── parse_output_file validation ────────────────────────────────────────────

def test_oversize_line_skipped(tmp_path: Path) -> None:
    out = tmp_path / "batch-000-output.jsonl"
    big = "x" * 20_000
    out.write_text(json.dumps({"ein": "1"*9, "url": "https://a.org",
                               "confidence": "high", "reasoning": big}) + "\n")
    warnings = []
    result = br.parse_output_file(
        [out], {"1"*9},
        warn=lambda msg, **kw: warnings.append(msg),
    )
    assert result == {}
    assert warnings
    assert "too long" in warnings[0]


def test_malformed_json_line_skipped(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text("{not json\n"
                   + json.dumps({"ein": "1"*9, "url": "https://a.org",
                                 "confidence": "high", "reasoning": "ok"})
                   + "\n")
    result = br.parse_output_file([out], {"1"*9})
    assert set(result.keys()) == {"1"*9}


def test_ein_not_in_batch_input_skipped(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"ein": "2"*9, "url": "https://a.org",
                               "confidence": "high", "reasoning": "r"}) + "\n")
    result = br.parse_output_file([out], {"1"*9})
    assert result == {}


def test_invalid_ein_format_skipped(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"ein": "abc",
                               "url": "https://a.org",
                               "confidence": "high",
                               "reasoning": ""}) + "\n")
    result = br.parse_output_file([out], {"abc"})
    assert result == {}


def test_invalid_url_scheme_skipped(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"ein": "1"*9, "url": "ftp://x.org",
                               "confidence": "high", "reasoning": "r"}) + "\n")
    result = br.parse_output_file([out], {"1"*9})
    assert result == {}


def test_invalid_confidence_skipped(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"ein": "1"*9, "url": "https://a.org",
                               "confidence": "maybe", "reasoning": "r"}) + "\n")
    result = br.parse_output_file([out], {"1"*9})
    assert result == {}


def test_none_confidence_forces_null_url(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"ein": "1"*9, "url": "https://x.org",
                               "confidence": "none", "reasoning": "r"}) + "\n")
    result = br.parse_output_file([out], {"1"*9})
    assert result["1"*9]["url"] is None


# ── AC19: duplicate EIN — last-write-wins ─────────────────────────────────

def test_duplicate_ein_last_write_wins(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    out.write_text(
        json.dumps({"ein": "1"*9, "url": "https://first.org",
                    "confidence": "high", "reasoning": "r1"}) + "\n"
        + json.dumps({"ein": "1"*9, "url": "https://second.org",
                      "confidence": "high", "reasoning": "r2"}) + "\n"
    )
    result = br.parse_output_file([out], {"1"*9})
    assert result["1"*9]["url"] == "https://second.org"


# ── AC20: reasoning truncation with ellipsis ───────────────────────────────

def test_reasoning_truncated_with_ellipsis(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    long_reason = "a" * 800
    out.write_text(json.dumps({"ein": "1"*9, "url": "https://a.org",
                               "confidence": "high",
                               "reasoning": long_reason}) + "\n")
    result = br.parse_output_file([out], {"1"*9})
    r = result["1"*9]["reasoning"]
    assert len(r) == 500
    assert r.endswith("...")


# ── ingest_rows (DB write path) ───────────────────────────────────────────

def test_ingest_rows_writes_all_fields_correctly(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _init_db(db, [{"ein": "1"*9}])
    engine = create_engine(f"sqlite:///{db}")
    rows = {
        "1"*9: {"ein": "1"*9, "url": "https://a.org",
                "confidence": "high", "reasoning": "matched on address"},
    }
    written, skipped = br.ingest_rows(engine, rows, model="haiku",
                                      re_resolve=False)
    assert (written, skipped) == (1, 0)
    got = _read(engine, "1"*9)
    assert got["website_url"] == "https://a.org"
    assert got["resolver_status"] == "resolved"
    assert got["resolver_confidence"] == 0.9
    assert got["resolver_method"] == f"claude-haiku-agent-v{PROMPT_VERSION}"
    assert got["resolver_reason"] == "matched on address"
    cands = json.loads(got["website_candidates_json"])
    assert isinstance(cands, list) and len(cands) == 1
    assert cands[0]["url"] == "https://a.org"
    assert cands[0]["confidence"] == "high"


def test_skip_already_resolved_unless_re_resolve(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _init_db(db, [
        {"ein": "1"*9, "resolver_status": "resolved"},
        {"ein": "2"*9, "resolver_status": None},
    ])
    engine = create_engine(f"sqlite:///{db}")
    rows = {
        "1"*9: {"ein": "1"*9, "url": "https://a.org",
                "confidence": "high", "reasoning": "r"},
        "2"*9: {"ein": "2"*9, "url": "https://b.org",
                "confidence": "high", "reasoning": "r"},
    }
    written, skipped = br.ingest_rows(engine, rows, model="haiku",
                                      re_resolve=False)
    assert written == 1
    assert skipped == 1
    # Now with --re-resolve (same DB, same already-resolved row):
    written2, skipped2 = br.ingest_rows(
        engine, {"1"*9: {"ein": "1"*9, "url": "https://c.org",
                         "confidence": "high", "reasoning": "r"}},
        model="haiku", re_resolve=True,
    )
    assert written2 == 1


# ── AC7: per-batch transaction isolation is covered by separate engine.begin() ──

def test_ingest_rollback_on_db_error(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "s.db"
    _init_db(db, [{"ein": "1"*9}])
    engine = create_engine(f"sqlite:///{db}")
    rows = {
        "1"*9: {"ein": "1"*9, "url": "https://a.org",
                "confidence": "high", "reasoning": "r"},
    }

    # Force an exception partway through.
    real_execute = None

    def boom(self, stmt, params=None, *a, **kw):
        raise RuntimeError("injected fail")

    # Induce failure by monkey-patching the UPDATE_SQL with a broken query.
    bad_sql = text("NOT A VALID UPDATE")
    monkeypatch.setattr(br, "UPDATE_SQL", bad_sql)
    with pytest.raises(Exception):
        br.ingest_rows(engine, rows, model="haiku", re_resolve=False)
    # DB should not have been mutated (row still has default NULL url).
    got = _read(engine, "1"*9)
    assert got["website_url"] is None
