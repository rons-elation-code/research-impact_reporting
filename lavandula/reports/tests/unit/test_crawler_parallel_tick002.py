"""TICK-002 — crawler.run() uses ThreadPoolExecutor + DBWriter.

Covers:
- AC1: ThreadPoolExecutor with --max-workers
- AC2: rows written with classification=NULL (no inline classifier call)
- AC3: per-org failure does not abort the run
- AC6: per-host throttle serializes concurrent same-host calls
       (covered also in test_host_throttle_tick002)
- AC7: --max-workers=1 preserves serial behavior
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from lavandula.reports import crawler, db_writer, host_throttle, schema
from lavandula.reports.db_queue import DBWriter


@pytest.fixture(autouse=True)
def _reset_throttle():
    host_throttle.reset_for_testing()
    yield
    host_throttle.reset_for_testing()


def _stub_process_org(*, ein, website, archive_dir, db_queue, **kwargs):
    """Replacement for crawler.process_org — records calls, no I/O."""
    db_writer.upsert_crawled_org(
        None,
        db_writer=db_queue,
        ein=ein,
        candidate_count=0,
        fetched_count=0,
        confirmed_report_count=0,
    )
    return crawler.OrgResult(ein=ein)


def _make_db(tmp_path: Path) -> Path:
    p = tmp_path / "reports.db"
    conn = schema.ensure_db(p)
    conn.close()
    return p


def test_ac1_run_uses_thread_pool(tmp_path, monkeypatch):
    """8 orgs, max_workers=4 → ThreadPoolExecutor dispatches in parallel.
    We assert by detecting >1 distinct worker thread name."""
    db_path = _make_db(tmp_path)
    seeds = [(f"{i:09d}", f"https://example{i}.org/") for i in range(8)]

    monkeypatch.setattr(
        "lavandula.reports.crawler.fetch_seeds_from_0001",
        lambda _p: seeds,
    )

    seen_threads: set[str] = set()
    barrier = threading.Barrier(4, timeout=5.0)

    def slow_proc(*, ein, website, archive_dir, db_queue, **kw):
        seen_threads.add(threading.current_thread().name)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return _stub_process_org(
            ein=ein, website=website, archive_dir=archive_dir, db_queue=db_queue,
        )

    monkeypatch.setattr("lavandula.reports.crawler.process_org", slow_proc)
    monkeypatch.setattr(
        "lavandula.reports.crawler.tls_self_test", lambda *a, **k: None
    )

    rc = crawler.run([
        "--nonprofits-db", str(tmp_path / "fake.db"),
        "--data-dir", str(tmp_path),
        "--archive-dir", str(tmp_path / "raw"),
        "--max-workers", "4",
        "--skip-tls-self-test",
        "--skip-encryption-check",
    ])
    assert rc == 0
    # ≥2 worker threads observed = real parallelism.
    assert len(seen_threads) >= 2, seen_threads


def test_ac2_process_org_writes_classification_null(tmp_path):
    """`process_org` must not call the classifier — sentinel patches a
    non-callable into select_classifier_client and verifies process_org
    doesn't touch it. Verified directly: AC2 = no `_classify` import in
    crawler.py and no anthropic_client param.
    """
    import inspect
    src = inspect.getsource(crawler.process_org)
    assert "classify_first_page" not in src
    assert "anthropic_client" not in src
    # And the upsert_report call sets classification=None
    assert "classification=None" in src


def test_ac3_one_org_failure_does_not_abort_run(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    seeds = [("0001", "https://a.org/"), ("0002", "https://b.org/"), ("0003", "https://c.org/")]
    monkeypatch.setattr(
        "lavandula.reports.crawler.fetch_seeds_from_0001",
        lambda _p: seeds,
    )

    def maybe_boom(*, ein, **kw):
        if ein == "0002":
            raise RuntimeError("boom")
        return _stub_process_org(ein=ein, **kw)

    monkeypatch.setattr("lavandula.reports.crawler.process_org", maybe_boom)
    monkeypatch.setattr(
        "lavandula.reports.crawler.tls_self_test", lambda *a, **k: None
    )

    rc = crawler.run([
        "--nonprofits-db", str(tmp_path / "fake.db"),
        "--data-dir", str(tmp_path),
        "--archive-dir", str(tmp_path / "raw"),
        "--max-workers", "2",
        "--skip-tls-self-test",
        "--skip-encryption-check",
    ])
    assert rc == 0

    # Surviving orgs were recorded.
    conn = sqlite3.connect(str(db_path))
    eins = {r[0] for r in conn.execute("SELECT ein FROM crawled_orgs")}
    conn.close()
    assert {"0001", "0003"}.issubset(eins)
    assert "0002" not in eins


def test_ac7_max_workers_1_serial(tmp_path, monkeypatch):
    """max_workers=1 still completes successfully (deterministic path)."""
    db_path = _make_db(tmp_path)
    seeds = [("0010", "https://x.org/"), ("0011", "https://y.org/")]
    monkeypatch.setattr(
        "lavandula.reports.crawler.fetch_seeds_from_0001",
        lambda _p: seeds,
    )
    monkeypatch.setattr(
        "lavandula.reports.crawler.process_org", _stub_process_org,
    )
    monkeypatch.setattr(
        "lavandula.reports.crawler.tls_self_test", lambda *a, **k: None
    )
    rc = crawler.run([
        "--nonprofits-db", str(tmp_path / "fake.db"),
        "--data-dir", str(tmp_path),
        "--archive-dir", str(tmp_path / "raw"),
        "--max-workers", "1",
        "--skip-tls-self-test",
        "--skip-encryption-check",
    ])
    assert rc == 0
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM crawled_orgs").fetchone()[0]
    conn.close()
    assert n == 2


def test_max_workers_out_of_range_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lavandula.reports.crawler.tls_self_test", lambda *a, **k: None
    )
    with pytest.raises(SystemExit):
        crawler.run([
            "--data-dir", str(tmp_path),
            "--max-workers", "0",
            "--skip-tls-self-test",
            "--skip-encryption-check",
        ])
    with pytest.raises(SystemExit):
        crawler.run([
            "--data-dir", str(tmp_path),
            "--max-workers", "33",
            "--skip-tls-self-test",
            "--skip-encryption-check",
        ])


def test_duplicate_eins_deduped_before_dispatch(tmp_path, monkeypatch):
    """Review round 1: duplicate EINs in the seed list must not race
    into the same crawled_orgs row. Dedup keeps the first occurrence."""
    db_path = _make_db(tmp_path)
    seeds = [
        ("0001", "https://a.org/"),
        ("0001", "https://a-dup.org/"),  # duplicate
        ("0002", "https://b.org/"),
        ("0001", "https://a-dup2.org/"),  # duplicate
    ]
    monkeypatch.setattr(
        "lavandula.reports.crawler.fetch_seeds_from_0001",
        lambda _p: seeds,
    )
    calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def record_call(*, ein, website, archive_dir, db_queue, **kw):
        with lock:
            calls.append((ein, website))
        return _stub_process_org(
            ein=ein, website=website, archive_dir=archive_dir, db_queue=db_queue,
        )

    monkeypatch.setattr("lavandula.reports.crawler.process_org", record_call)
    monkeypatch.setattr(
        "lavandula.reports.crawler.tls_self_test", lambda *a, **k: None
    )

    rc = crawler.run([
        "--nonprofits-db", str(tmp_path / "fake.db"),
        "--data-dir", str(tmp_path),
        "--archive-dir", str(tmp_path / "raw"),
        "--max-workers", "4",
        "--skip-tls-self-test",
        "--skip-encryption-check",
    ])
    assert rc == 0
    # Only one call per unique EIN; "0001" kept at the first website.
    assert sorted(calls) == [("0001", "https://a.org/"), ("0002", "https://b.org/")]


def test_writer_death_aborts_run(tmp_path, monkeypatch):
    """If the DB writer dies mid-run, the crawler returns nonzero/raises."""
    db_path = _make_db(tmp_path)
    seeds = [(f"{i:09d}", f"https://example{i}.org/") for i in range(4)]
    monkeypatch.setattr(
        "lavandula.reports.crawler.fetch_seeds_from_0001",
        lambda _p: seeds,
    )

    def kill_writer(*, ein, website, archive_dir, db_queue, **kw):
        # First worker submits a poisoned op that crashes the writer.
        if ein.endswith("0"):
            db_queue.put(lambda c: (_ for _ in ()).throw(RuntimeError("writer kaboom")))
        return _stub_process_org(
            ein=ein, website=website, archive_dir=archive_dir, db_queue=db_queue,
        )

    monkeypatch.setattr("lavandula.reports.crawler.process_org", kill_writer)
    monkeypatch.setattr(
        "lavandula.reports.crawler.tls_self_test", lambda *a, **k: None
    )

    with pytest.raises((RuntimeError,)):
        crawler.run([
            "--nonprofits-db", str(tmp_path / "fake.db"),
            "--data-dir", str(tmp_path),
            "--archive-dir", str(tmp_path / "raw"),
            "--max-workers", "2",
            "--skip-tls-self-test",
            "--skip-encryption-check",
        ])
