"""Tests for async_crawler.py (AC22, AC23, AC24, AC34, AC36, AC26 parity)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from lavandula.reports.async_crawler import (
    CrawlStats,
    _is_transient,
    _process_org_async,
)
from lavandula.reports.async_db_writer import (
    DBWriterActor,
    OrgDownloadTracker,
    RecordFetchRequest,
    UpsertCrawledOrgRequest,
)
from lavandula.reports.async_discover import DiscoveryResult
from lavandula.reports.candidate_filter import Candidate


@dataclass
class _FakeResult:
    body: bytes | None = None
    status: str = "ok"
    http_status: int | None = 200
    final_url: str = ""
    final_url_redacted: str = ""
    note: str = ""
    redirect_chain: list[str] | None = None
    redirect_chain_redacted: list[str] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    kind: str = ""
    elapsed_ms: int = 0
    error: str | None = None
    bytes_read: int = 0


# ---------- AC23: transient discovery failures ----------

@pytest.mark.asyncio
async def test_transient_discovery_no_crawled_org_row():
    """AC23: org with unreachable homepage + 0 candidates should NOT get a
    crawled_orgs row, so resume will retry it."""
    enqueued: list[object] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        enqueued.append(req)
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    db_actor = MagicMock()
    db_actor.enqueue = mock_enqueue

    async def mock_get(url, *, kind="homepage", seed_etld1=None, extra_headers=None):
        return _FakeResult(body=b"", status="network_error", http_status=0,
                           final_url=url, final_url_redacted=url, note="conn refused")

    client = MagicMock()
    client.get = mock_get

    stats = CrawlStats()

    discovery_result = DiscoveryResult(
        candidates=[], homepage_ok=False, robots_disallowed_all=False,
    )

    with patch(
        "lavandula.reports.async_crawler.discover_org",
        return_value=discovery_result,
    ):
        await _process_org_async(
            ein="99-0000000",
            website="https://down.org",
            client=client,
            db_actor=db_actor,
            download_queue=asyncio.Queue(),
            archive=MagicMock(),
            run_id="test",
            stats=stats,
            shutdown_event=asyncio.Event(),
            pdf_thread_pool=MagicMock(),
        )

    crawled_org_writes = [r for r in enqueued if isinstance(r, UpsertCrawledOrgRequest)]
    assert len(crawled_org_writes) == 0
    assert stats.orgs_transient_failed == 1


@pytest.mark.asyncio
async def test_successful_discovery_writes_crawled_org():
    """Normal crawl (homepage ok, candidates found) should write a crawled_orgs row."""
    enqueued: list[object] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        enqueued.append(req)
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    db_actor = MagicMock()
    db_actor.enqueue = mock_enqueue

    async def mock_get(url, *, kind="homepage", seed_etld1=None, extra_headers=None):
        return _FakeResult(body=b"robots", status="ok", http_status=200,
                           final_url=url, final_url_redacted=url)

    client = MagicMock()
    client.get = mock_get

    stats = CrawlStats()

    discovery_result = DiscoveryResult(
        candidates=[], homepage_ok=True, robots_disallowed_all=False,
    )

    with patch(
        "lavandula.reports.async_crawler.discover_org",
        return_value=discovery_result,
    ):
        await _process_org_async(
            ein="12-3456789",
            website="https://example.com",
            client=client,
            db_actor=db_actor,
            download_queue=asyncio.Queue(),
            archive=MagicMock(),
            run_id="test",
            stats=stats,
            shutdown_event=asyncio.Event(),
            pdf_thread_pool=MagicMock(),
        )

    crawled_org_writes = [r for r in enqueued if isinstance(r, UpsertCrawledOrgRequest)]
    assert len(crawled_org_writes) == 1
    assert stats.orgs_completed == 1


# ---------- AC24: permanent failure classification ----------

def test_is_transient_connector_error():
    exc = ConnectionError("refused")
    assert _is_transient(exc) is True


def test_is_transient_timeout():
    exc = asyncio.TimeoutError()
    assert _is_transient(exc) is True


def test_is_not_transient_value_error():
    exc = ValueError("bad")
    assert _is_transient(exc) is False


def test_is_not_transient_runtime_error():
    exc = RuntimeError("SSRF blocked")
    assert _is_transient(exc) is False


# ---------- AC34: exit code propagation ----------

def test_crawl_stats_exit_code_fields():
    stats = CrawlStats()
    assert stats.flush_failures == 0
    assert stats.exit_code == 0

    stats.flush_failures = 3
    stats.exit_code = 1
    assert stats.exit_code == 1


# ---------- AC36: download_queue_depth ----------

def test_crawl_stats_download_queue_depth():
    stats = CrawlStats()
    assert stats.download_queue_depth == 0
    stats.download_queue_depth = 42
    assert stats.download_queue_depth == 42


# ---------- AC22: ordering (no crawled_orgs until downloads flushed) ----------

@pytest.mark.asyncio
async def test_org_completion_waits_for_downloads():
    """AC22: UpsertCrawledOrgRequest must not be enqueued until all downloads
    for that org have completed (OrgDownloadTracker barrier)."""
    event_log: list[str] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        if isinstance(req, UpsertCrawledOrgRequest):
            event_log.append("crawled_org_enqueued")
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    db_actor = MagicMock()
    db_actor.enqueue = mock_enqueue

    async def mock_get(url, *, kind="homepage", seed_etld1=None, extra_headers=None):
        return _FakeResult(body=b"ok", status="ok", http_status=200,
                           final_url=url, final_url_redacted=url)

    client = MagicMock()
    client.get = mock_get

    download_queue = asyncio.Queue()
    stats = CrawlStats()
    shutdown = asyncio.Event()

    cand = Candidate(
        url="https://example.com/report.pdf",
        anchor_text="Annual Report",
        referring_page_url="https://example.com",
        discovered_via="homepage-link",
        hosting_platform="own-domain",
        attribution_confidence="high",
    )
    discovery_result = DiscoveryResult(
        candidates=[cand], homepage_ok=True,
    )

    async def fake_consume():
        await asyncio.sleep(0.05)
        item = await download_queue.get()
        ein, c, tracker, etld, org_fetched = item
        event_log.append("download_done")
        tracker.decrement()
        download_queue.task_done()

    consumer_task = asyncio.create_task(fake_consume())

    with patch(
        "lavandula.reports.async_crawler.discover_org",
        return_value=discovery_result,
    ):
        await _process_org_async(
            ein="12-3456789",
            website="https://example.com",
            client=client,
            db_actor=db_actor,
            download_queue=download_queue,
            archive=MagicMock(),
            run_id="test",
            stats=stats,
            shutdown_event=shutdown,
            pdf_thread_pool=MagicMock(),
        )

    await consumer_task

    assert event_log.index("download_done") < event_log.index("crawled_org_enqueued")
