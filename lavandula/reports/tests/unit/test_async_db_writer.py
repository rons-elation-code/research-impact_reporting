"""Tests for async_db_writer.py (AC18, AC19, AC22, AC39)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from lavandula.reports.async_db_writer import (
    DBWriterActor,
    OrgDownloadTracker,
    RecordFetchRequest,
    UpsertCrawledOrgRequest,
    UpsertReportRequest,
)


@pytest.mark.asyncio
async def test_org_download_tracker_immediate_done():
    tracker = OrgDownloadTracker()
    await tracker.wait_all_done()


@pytest.mark.asyncio
async def test_org_download_tracker_barrier():
    tracker = OrgDownloadTracker()
    tracker.increment()
    tracker.increment()

    done = False

    async def waiter():
        nonlocal done
        await tracker.wait_all_done()
        done = True

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert not done

    tracker.decrement()
    await asyncio.sleep(0.01)
    assert not done

    tracker.decrement()
    await asyncio.sleep(0.01)
    assert done

    await task


@pytest.mark.asyncio
async def test_org_download_tracker_decrement_below_zero():
    tracker = OrgDownloadTracker()
    tracker.increment()
    tracker.decrement()
    tracker.decrement()
    await tracker.wait_all_done()


@pytest.mark.asyncio
async def test_db_actor_batch_flush():
    engine = MagicMock()

    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        actor = DBWriterActor(engine, batch_size=3, flush_interval_sec=0.1)
        task = asyncio.create_task(actor.run())

        f1 = await actor.enqueue(RecordFetchRequest(
            ein="111", url_redacted="u1", kind="robots",
            fetch_status="ok",
        ))
        f2 = await actor.enqueue(RecordFetchRequest(
            ein="222", url_redacted="u2", kind="robots",
            fetch_status="ok",
        ))
        f3 = await actor.enqueue(RecordFetchRequest(
            ein="333", url_redacted="u3", kind="robots",
            fetch_status="ok",
        ))

        await asyncio.sleep(0.2)

        assert mock_db.record_fetch.call_count == 3

        await actor.flush_and_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_passes_status_to_upsert_crawled_org():
    """Spec 0021 follow-up: status='transient' / 'permanent_skip' must
    flow through DBWriterActor → db_writer.upsert_crawled_org so the
    SQL CASE can decide auto-promotion to 'permanent_skip'."""
    engine = MagicMock()
    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        actor = DBWriterActor(engine, batch_size=10, flush_interval_sec=0.1)
        task = asyncio.create_task(actor.run())

        for status in ("ok", "transient", "permanent_skip"):
            await actor.enqueue(UpsertCrawledOrgRequest(
                ein=f"ein-{status}",
                candidate_count=0,
                fetched_count=0,
                confirmed_report_count=0,
                status=status,
            ))

        await actor.flush_and_stop()

        # Each call passed status= kwarg through.
        seen_statuses = sorted(
            call.kwargs["status"] for call in mock_db.upsert_crawled_org.call_args_list
        )
        assert seen_statuses == ["ok", "permanent_skip", "transient"]

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_flush_on_timeout():
    engine = MagicMock()

    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        actor = DBWriterActor(engine, batch_size=100, flush_interval_sec=0.1)
        task = asyncio.create_task(actor.run())

        await actor.enqueue(RecordFetchRequest(
            ein="111", url_redacted="u1", kind="robots",
            fetch_status="ok",
        ))

        await asyncio.sleep(0.3)
        assert mock_db.record_fetch.call_count >= 1

        await actor.flush_and_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_future_resolves():
    engine = MagicMock()

    with patch("lavandula.reports.async_db_writer.db_writer"):
        actor = DBWriterActor(engine, batch_size=1, flush_interval_sec=0.1)
        task = asyncio.create_task(actor.run())

        future = await actor.enqueue(RecordFetchRequest(
            ein="111", url_redacted="u1", kind="robots",
            fetch_status="ok",
        ))

        result = await asyncio.wait_for(future, timeout=2.0)
        assert result is True

        await actor.flush_and_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_retry_on_failure():
    engine = MagicMock()

    call_count = [0]

    def failing_record_fetch(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("db error")

    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        mock_db.record_fetch.side_effect = failing_record_fetch
        actor = DBWriterActor(engine, batch_size=1, flush_interval_sec=0.1)
        task = asyncio.create_task(actor.run())

        future = await actor.enqueue(RecordFetchRequest(
            ein="111", url_redacted="u1", kind="robots",
            fetch_status="ok",
        ))

        result = await asyncio.wait_for(future, timeout=2.0)
        assert result is True
        assert call_count[0] == 2

        await actor.flush_and_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_persistent_failure():
    engine = MagicMock()

    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        mock_db.record_fetch.side_effect = RuntimeError("permanent db error")
        actor = DBWriterActor(engine, batch_size=1, flush_interval_sec=0.1)
        task = asyncio.create_task(actor.run())

        future = await actor.enqueue(RecordFetchRequest(
            ein="111", url_redacted="u1", kind="robots",
            fetch_status="ok",
        ))

        # New behavior (post per-row-isolation refactor): the underlying
        # DB exception propagates directly, so callers see the real error
        # instead of an opaque "DB flush failed" wrapper.
        with pytest.raises(RuntimeError, match="permanent db error"):
            await asyncio.wait_for(future, timeout=2.0)

        assert actor.flush_failures > 0

        await actor.flush_and_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_per_row_isolation_one_bad_row():
    """One bad row in a batch must NOT poison the other rows' futures.

    Regression test for the NUL-byte scenario where async amplified a
    single-row failure into a whole-batch failure (~17 reports lost in
    one transaction). With per-row isolation, bad row #2 fails alone
    and the other 4 rows still resolve True.
    """
    engine = MagicMock()
    call_seq = []

    def selectively_failing_record_fetch(*args, **kwargs):
        url = kwargs.get("url_redacted", "")
        call_seq.append(url)
        if url == "BAD":
            raise RuntimeError(
                "A string literal cannot contain NUL (0x00) characters."
            )

    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        mock_db.record_fetch.side_effect = selectively_failing_record_fetch
        actor = DBWriterActor(engine, batch_size=10, flush_interval_sec=0.5)
        task = asyncio.create_task(actor.run())

        urls = ["url0", "url1", "BAD", "url3", "url4"]
        futures = []
        for u in urls:
            fut = await actor.enqueue(RecordFetchRequest(
                ein="111", url_redacted=u, kind="robots", fetch_status="ok",
            ))
            futures.append((u, fut))

        await asyncio.sleep(1.0)  # give the timer flush room to fire

        results = []
        for u, fut in futures:
            try:
                v = await asyncio.wait_for(fut, timeout=2.0)
                results.append((u, "ok", v))
            except Exception as exc:
                results.append((u, "err", str(exc)))

        good = [r for r in results if r[0] != "BAD"]
        bad = [r for r in results if r[0] == "BAD"]

        assert all(r[1] == "ok" for r in good), (
            f"good rows must succeed; got {good}"
        )
        assert all(r[1] == "err" for r in bad), (
            f"bad row must fail; got {bad}"
        )
        assert "NUL" in bad[0][2], f"failure should surface DB error: {bad[0][2]}"
        assert actor.flush_failures == 1

        await actor.flush_and_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_db_actor_flush_and_stop_drains():
    engine = MagicMock()

    with patch("lavandula.reports.async_db_writer.db_writer") as mock_db:
        actor = DBWriterActor(engine, batch_size=100, flush_interval_sec=10.0)
        task = asyncio.create_task(actor.run())

        for i in range(5):
            await actor.enqueue(RecordFetchRequest(
                ein=str(i), url_redacted=f"u{i}", kind="robots",
                fetch_status="ok",
            ))

        await actor.flush_and_stop()
        assert mock_db.record_fetch.call_count == 5

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
