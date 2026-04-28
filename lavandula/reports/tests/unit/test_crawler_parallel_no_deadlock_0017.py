"""Spec 0017 — parallel db_writer calls against Postgres must not deadlock.

Spawns 8 threads concurrently writing `fetch_log`, `crawled_orgs`, and
`reports` rows; asserts the run completes within a generous timeout
and that every write landed.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("postgres_engine")


def test_parallel_fetch_log_writes(postgres_engine):
    from lavandula.reports import db_writer

    N = 8
    per_thread = 12
    errors: list[BaseException] = []

    def worker(tid: int):
        try:
            for i in range(per_thread):
                db_writer.record_fetch(
                    postgres_engine,
                    ein=f"00000{tid:02d}{i:02d}",
                    url_redacted=f"https://example.org/t{tid}/i{i}",
                    kind="homepage",
                    fetch_status="ok",
                    status_code=200,
                    elapsed_ms=10,
                    notes=None,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - t0

    assert elapsed < 30, f"deadlock suspected: elapsed={elapsed:.1f}s"
    assert not errors, f"unexpected errors: {errors!r}"

    with postgres_engine.connect() as conn:
        count = int(conn.execute(text(
            "SELECT COUNT(*) FROM lava_corpus.fetch_log"
        )).scalar() or 0)
    assert count == N * per_thread


def test_parallel_upsert_crawled_org_merge(postgres_engine):
    from lavandula.reports import db_writer

    # Multiple threads upsert the same EIN concurrently; the final
    # state must reflect the maximum confirmed_report_count across all
    # concurrent writers (GREATEST semantics).
    ein = "123456789"
    errors: list[BaseException] = []

    def worker(confirmed: int):
        try:
            db_writer.upsert_crawled_org(
                postgres_engine,
                ein=ein,
                candidate_count=5,
                fetched_count=3,
                confirmed_report_count=confirmed,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,))
               for c in (0, 2, 5, 1, 3, 4, 0, 0)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    with postgres_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT confirmed_report_count FROM lava_corpus.crawled_orgs "
            "WHERE ein = :e"
        ), {"e": ein}).fetchone()
    # Final state must be the max confirmed passed by any thread.
    assert int(row[0]) == 5
