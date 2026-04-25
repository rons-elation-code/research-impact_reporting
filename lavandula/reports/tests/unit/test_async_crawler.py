"""Tests for async_crawler.py (AC22, AC23, AC24, AC34, AC36, AC26 parity)."""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from lavandula.reports.async_crawler import (
    CrawlStats,
    _download_worker,
    _is_transient,
    _process_org_async,
    run_async,
)
from lavandula.reports.async_db_writer import (
    DBWriterActor,
    OrgDownloadTracker,
    RecordFetchRequest,
    UpsertCrawledOrgRequest,
    UpsertReportRequest,
)
from lavandula.reports.async_discover import DiscoveryResult
from lavandula.reports.candidate_filter import Candidate
from lavandula.reports.fetch_pdf import DownloadOutcome


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
async def test_transient_discovery_writes_transient_row():
    """AC23 + retry-cap follow-up: org with unreachable homepage + 0
    candidates writes a row with status='transient' so attempts can be
    counted. The upsert SQL auto-promotes to 'permanent_skip' once
    attempts >= MAX_TRANSIENT_ATTEMPTS, so retry behavior is preserved
    while still bounding the retry budget."""
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
    assert len(crawled_org_writes) == 1
    assert crawled_org_writes[0].status == "transient"
    assert crawled_org_writes[0].candidate_count == 0
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


# ---------- AC22: slow-flush durability (must-fix round 2) ----------

@pytest.mark.asyncio
async def test_ac22_crawled_org_waits_for_report_durability():
    """upsert_crawled_org must NOT be enqueued until upsert_report futures resolve.

    Injects a slow-resolving future for UpsertReportRequest to verify that
    _process_download awaits report_future before returning, which in turn
    means OrgDownloadTracker.decrement happens AFTER the report flush.
    """
    timestamps: list[tuple[str, float]] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        if isinstance(req, UpsertReportRequest):
            fut = loop.create_future()

            async def _resolve_later():
                await asyncio.sleep(0.15)
                timestamps.append(("report_flushed", loop.time()))
                fut.set_result(True)

            asyncio.create_task(_resolve_later())
            return fut
        if isinstance(req, UpsertCrawledOrgRequest):
            timestamps.append(("crawled_org_enqueued", loop.time()))
            fut = loop.create_future()
            fut.set_result(True)
            return fut
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

    download_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    stats = CrawlStats()
    pdf_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-pdf")

    cand = Candidate(
        url="https://example.com/report.pdf",
        anchor_text="Annual Report",
        referring_page_url="https://example.com",
        discovered_via="homepage-link",
        hosting_platform="own-domain",
        attribution_confidence="high",
    )
    discovery_result = DiscoveryResult(candidates=[cand], homepage_ok=True)

    fake_outcome = DownloadOutcome(
        status="ok",
        url="https://example.com/report.pdf",
        final_url="https://example.com/report.pdf",
        final_url_redacted="https://example.com/report.pdf",
        redirect_chain=["https://example.com/report.pdf"],
        redirect_chain_redacted=["https://example.com/report.pdf"],
        content_sha256="abc123",
        bytes_read=1000,
        content_type="application/pdf",
        body=b"%PDF-1.4 fake content",
    )

    fake_archive = MagicMock()
    fake_archive.put = MagicMock()

    worker_task = asyncio.create_task(
        _download_worker(
            download_queue, client, db_actor, fake_archive,
            "test-run", stats, pdf_pool,
        )
    )

    with patch(
        "lavandula.reports.async_crawler.discover_org",
        return_value=discovery_result,
    ), patch(
        "lavandula.reports.async_crawler.async_download",
        return_value=fake_outcome,
    ), patch(
        "lavandula.reports.async_crawler.scan_active_content",
        return_value={"pdf_has_javascript": 0, "pdf_has_launch": 0,
                      "pdf_has_embedded": 0, "pdf_has_uri_actions": 0},
    ):
        await _process_org_async(
            ein="12-3456789",
            website="https://example.com",
            client=client,
            db_actor=db_actor,
            download_queue=download_queue,
            archive=fake_archive,
            run_id="test-run",
            stats=stats,
            shutdown_event=asyncio.Event(),
            pdf_thread_pool=pdf_pool,
        )

    await download_queue.put(None)
    await worker_task
    pdf_pool.shutdown(wait=False)

    report_ts = [ts for label, ts in timestamps if label == "report_flushed"]
    org_ts = [ts for label, ts in timestamps if label == "crawled_org_enqueued"]
    assert len(report_ts) >= 1, "report_flushed event not seen"
    assert len(org_ts) >= 1, "crawled_org_enqueued event not seen"
    assert report_ts[0] < org_ts[0], (
        f"report flush ({report_ts[0]}) should happen before "
        f"crawled_org enqueue ({org_ts[0]})"
    )


# ---------- Shutdown integration test (must-fix round 2) ----------

@pytest.mark.asyncio
async def test_sigint_mid_flight_drains_cleanly():
    """Real SIGINT mid-flight: producer stops, in-flight orgs finish,
    DB drains, fewer than all seeds completed, exit_code=0, and every
    completed org has exactly one crawled_orgs row (no half-writes)."""
    import os
    import signal
    import tempfile
    from pathlib import Path

    enqueued: list[object] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        enqueued.append(req)
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    async def slow_discover(*args, **kwargs):
        await asyncio.sleep(0.1)
        return DiscoveryResult(candidates=[], homepage_ok=True)

    async def mock_get(url, *, kind="homepage", seed_etld1=None, extra_headers=None):
        return _FakeResult(body=b"ok", status="ok", http_status=200,
                           final_url=url, final_url_redacted=url)

    client_mock = MagicMock()
    client_mock.get = mock_get

    seeds = [(f"EIN-{i:04d}", f"https://org{i}.example.com") for i in range(20)]

    with tempfile.TemporaryDirectory() as tmpdir:
        halt_dir = Path(tmpdir) / "halt"
        halt_dir.mkdir()

        with patch(
            "lavandula.reports.async_crawler.discover_org",
            side_effect=slow_discover,
        ), patch(
            "lavandula.reports.async_crawler.AsyncHTTPClient",
        ) as mock_client_cls, patch(
            "lavandula.reports.async_crawler.DBWriterActor",
        ) as mock_db_cls, patch(
            "lavandula.reports.async_crawler.AsyncHostThrottle",
        ), patch(
            "lavandula.reports.async_crawler.AsyncHostPinCache",
        ):
            mock_client_instance = MagicMock()
            mock_client_instance.__aenter__ = AsyncMock(return_value=client_mock)
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client_instance

            mock_db_instance = MagicMock()
            mock_db_instance.enqueue = mock_enqueue
            mock_db_instance.flush_failures = 0
            mock_db_instance.flush_and_stop = AsyncMock()

            async def mock_db_run():
                try:
                    await asyncio.sleep(999)
                except asyncio.CancelledError:
                    pass

            mock_db_instance.run = mock_db_run
            mock_db_cls.return_value = mock_db_instance

            async def send_sigint_soon():
                await asyncio.sleep(0.25)
                os.kill(os.getpid(), signal.SIGINT)

            asyncio.create_task(send_sigint_soon())

            stats = await run_async(
                engine=MagicMock(),
                archive=MagicMock(),
                seeds=seeds,
                max_concurrent_orgs=3,
                max_download_workers=2,
                run_id="shutdown-test",
                halt_dir=halt_dir,
            )

    assert stats.exit_code == 0
    assert stats.orgs_completed < len(seeds), (
        f"expected fewer than {len(seeds)} completed, got {stats.orgs_completed} — "
        "shutdown didn't interrupt the crawl"
    )
    assert stats.orgs_completed > 0, "at least one org should have completed before SIGINT"
    crawled_org_writes = [r for r in enqueued if isinstance(r, UpsertCrawledOrgRequest)]
    assert len(crawled_org_writes) == stats.orgs_completed, (
        f"crawled_org writes ({len(crawled_org_writes)}) != orgs_completed "
        f"({stats.orgs_completed}) — partial/missing writes detected"
    )
