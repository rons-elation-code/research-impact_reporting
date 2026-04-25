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

        with pytest.raises(RuntimeError, match="DB flush failed"):
            await asyncio.wait_for(future, timeout=2.0)

        assert actor.flush_failures > 0

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
