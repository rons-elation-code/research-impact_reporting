"""Argv validation tests for batch_resolve (Spec 0008).

Covers AC6, AC11, AC16.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from lavandula.nonprofits.tools import batch_resolve as br


def _parse(argv: list[str]):
    parser = br._build_parser()
    args = parser.parse_args(argv)
    br._validate_args(parser, args)
    return args


def test_batch_size_must_be_within_limits() -> None:
    with pytest.raises(SystemExit):
        _parse(["--db", "x.db", "--batch-size", "0", "--yes"])
    with pytest.raises(SystemExit):
        _parse(["--db", "x.db", "--batch-size", "51", "--yes"])


def test_parallelism_capped_at_4() -> None:
    with pytest.raises(SystemExit):
        _parse(["--db", "x.db", "--parallelism", "5", "--yes"])


def test_max_orgs_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        _parse(["--db", "x.db", "--max-orgs", "0", "--yes"])


def test_db_required_unless_resume() -> None:
    with pytest.raises(SystemExit):
        _parse([])


def test_resume_without_db_ok() -> None:
    args = _parse(["--resume", "/tmp/run-x"])
    assert args.resume == "/tmp/run-x"


def test_state_csv_normalizes_to_upper_list() -> None:
    args = _parse(["--db", "x.db", "--state", "ny,ma"])
    br._normalize_filters(args)
    assert args.state == ["NY", "MA"]


def test_non_tty_without_yes_exits_2(tmp_path: Path, monkeypatch) -> None:
    # Build an eligible DB and attempt to run without --yes.
    db = tmp_path / "seeds.db"
    _setup_db(db, _org_row())
    args = _parse(["--db", str(db)])
    # Simulate non-TTY stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = br.run(args)
    assert rc == 2


def test_prompt_n_input_aborts_with_exit_1(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "seeds.db"
    _setup_db(db, _org_row())
    args = _parse(["--db", str(db)])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(br, "_confirm_interactively", lambda: False)
    rc = br.run(args)
    assert rc == 1


def test_prompt_y_input_proceeds(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "seeds.db"
    _setup_db(db, _org_row())
    args = _parse(["--db", str(db), "--max-orgs", "1", "--batch-size", "1",
                   "--parallelism", "1"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(br, "_confirm_interactively", lambda: True)

    from lavandula.nonprofits.agent_runner import FakeAgentRunner
    rc = br.run(args, agent_runner_factory=lambda: FakeAgentRunner())
    assert rc == 0


# ── AC6: --max-orgs hard cap ─────────────────────────────────────────────────

def test_max_orgs_truncates_eligible_set(tmp_path: Path, monkeypatch, caplog) -> None:
    db = tmp_path / "seeds.db"
    _setup_db(db, *(_org_row(ein=f"{i:09d}") for i in range(5)))
    args = _parse(["--db", str(db), "--max-orgs", "2", "--batch-size", "2",
                   "--parallelism", "1", "--yes"])
    from lavandula.nonprofits.agent_runner import FakeAgentRunner
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        rc = br.run(args, agent_runner_factory=lambda: FakeAgentRunner())
    assert rc == 0
    assert any("truncation" in rec.message for rec in caplog.records)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _org_row(ein: str = "741394418", name: str = "Columbus Community Hospital",
             address: str = "110 Shult Dr", city: str = "Columbus",
             state: str = "TX", zipcode: str = "78934",
             ntee_code: str = "E20", revenue: int = 5_000_000) -> dict:
    return dict(ein=ein, name=name, address=address, city=city,
                state=state, zipcode=zipcode, ntee_code=ntee_code,
                revenue=revenue)


def _setup_db(path: Path, *rows: dict) -> None:
    import sqlite3
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
            "(ein,name,address,city,state,zipcode,ntee_code,revenue) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (r["ein"], r["name"], r["address"], r["city"], r["state"],
             r["zipcode"], r["ntee_code"], r["revenue"]),
        )
    conn.commit()
    conn.close()
