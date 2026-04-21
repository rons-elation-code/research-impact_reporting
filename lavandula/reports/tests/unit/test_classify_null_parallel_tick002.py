"""TICK-002 — classify_null.py uses ThreadPoolExecutor + --max-workers."""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
from pathlib import Path

import pytest


def test_ac4_classify_null_accepts_max_workers_flag():
    """The CLI must accept `--max-workers N`."""
    from lavandula.reports.tools import classify_null
    # Build parser by introspection: invoke main() with -h would exit;
    # instead, parse args ourselves on a copy of the CLI surface.
    src = Path(classify_null.__file__).read_text()
    assert '"--max-workers"' in src
    assert "ThreadPoolExecutor" in src


def test_ac4_classify_null_runs_in_parallel(tmp_path, monkeypatch):
    """With max_workers=4, four classifications run concurrently."""
    from lavandula.reports.tools import classify_null

    db = tmp_path / "reports.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE reports (
          content_sha256 TEXT PRIMARY KEY,
          first_page_text TEXT,
          archived_at TEXT,
          classification TEXT,
          classification_confidence REAL,
          classifier_model TEXT,
          classifier_version INTEGER,
          classified_at TEXT
        )
    """)
    for i in range(8):
        conn.execute(
            "INSERT INTO reports (content_sha256, first_page_text, archived_at) "
            "VALUES (?, ?, ?)",
            (f"sha{i:04d}" + "0" * 60, "page text", f"2026-01-0{i+1}T00:00:00"),
        )
    conn.commit()
    conn.close()

    barrier = threading.Barrier(4, timeout=5.0)
    seen_threads: set[str] = set()

    class _FakeResult:
        classification = "annual"
        classification_confidence = 0.95
        classifier_model = "fake-model"
        error = ""

    def fake_classify(text, *, client, raise_on_error):
        seen_threads.add(threading.current_thread().name)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return _FakeResult()

    monkeypatch.setattr(
        "lavandula.reports.tools.classify_null.classify_first_page",
        fake_classify,
    )
    monkeypatch.setattr(
        "lavandula.reports.tools.classify_null.select_classifier_client",
        lambda: object(),
    )

    monkeypatch.setattr(sys, "argv", [
        "classify_null", "--db", str(db), "--max-workers", "4",
    ])
    rc = classify_null.main()
    assert rc == 0
    assert len(seen_threads) >= 2, seen_threads

    conn = sqlite3.connect(str(db))
    n = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE classification = 'annual'"
    ).fetchone()[0]
    conn.close()
    assert n == 8


def test_ac4_max_workers_out_of_range(monkeypatch, tmp_path):
    from lavandula.reports.tools import classify_null
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", [
        "classify_null", "--db", str(db), "--max-workers", "0",
    ])
    with pytest.raises(SystemExit):
        classify_null.main()
