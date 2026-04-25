"""DB Writer Actor for async crawler (Spec 0021, AC18/AC19/AC22/AC39).

Single coroutine owning all DB writes. Write requests are typed
dataclasses; the actor batches them and flushes via run_in_executor
on a single-thread pool. Each enqueue returns a Future that resolves
when the write is durably flushed — callers can await it for
durability guarantees (org completion barrier).
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Engine

from . import db_writer

_log = logging.getLogger(__name__)


@dataclass
class RecordFetchRequest:
    op: str = field(default="record_fetch", init=False)
    ein: str | None = None
    url_redacted: str = ""
    kind: str = ""
    fetch_status: str = ""
    status_code: int | None = None
    elapsed_ms: int | None = None
    notes: str | None = None


@dataclass
class UpsertReportRequest:
    op: str = field(default="upsert_report", init=False)
    content_sha256: str = ""
    source_url_redacted: str = ""
    referring_page_url_redacted: str | None = None
    redirect_chain_redacted: list[str] | None = None
    source_org_ein: str = ""
    discovered_via: str = ""
    hosting_platform: str | None = None
    attribution_confidence: str = ""
    content_type: str = "application/pdf"
    file_size_bytes: int = 0
    page_count: int | None = None
    first_page_text: str | None = None
    pdf_creator: str | None = None
    pdf_producer: str | None = None
    pdf_creation_date: str | None = None
    pdf_has_javascript: int = 0
    pdf_has_launch: int = 0
    pdf_has_embedded: int = 0
    pdf_has_uri_actions: int = 0
    classification: str | None = None
    classification_confidence: float | None = None
    classifier_model: str = ""
    classifier_version: int = 0
    report_year: int | None = None
    report_year_source: str | None = None
    extractor_version: int = 0


@dataclass
class UpsertCrawledOrgRequest:
    op: str = field(default="upsert_crawled_org", init=False)
    ein: str = ""
    candidate_count: int = 0
    fetched_count: int = 0
    confirmed_report_count: int = 0


WriteRequest = RecordFetchRequest | UpsertReportRequest | UpsertCrawledOrgRequest


class OrgDownloadTracker:
    """Tracks outstanding downloads for one org. Barrier fires when all done."""

    def __init__(self) -> None:
        self._pending = 0
        self._done = asyncio.Event()
        self._done.set()

    def increment(self) -> None:
        self._pending += 1
        self._done.clear()

    def decrement(self) -> None:
        self._pending -= 1
        if self._pending <= 0:
            self._pending = 0
            self._done.set()

    async def wait_all_done(self) -> None:
        await self._done.wait()


class DBWriterActor:
    """Single coroutine that owns all DB writes."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_queue: int = 200,
        batch_size: int = 50,
        flush_interval_sec: float = 5.0,
    ) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[tuple[WriteRequest, asyncio.Future[bool]] | None] = (
            asyncio.Queue(maxsize=max_queue)
        )
        self._batch_size = batch_size
        self._flush_interval_sec = flush_interval_sec
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._flush_failures = 0

    async def enqueue(self, request: WriteRequest) -> asyncio.Future[bool]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        await self._queue.put((request, future))
        return future

    async def run(self) -> None:
        batch: list[tuple[WriteRequest, asyncio.Future[bool]]] = []
        while True:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=self._flush_interval_sec
                )
            except asyncio.TimeoutError:
                if batch:
                    await self._flush_batch(batch)
                    batch = []
                continue

            if item is None:
                self._queue.task_done()
                break

            batch.append(item)
            self._queue.task_done()

            while not self._queue.empty() and len(batch) < self._batch_size:
                try:
                    next_item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_item is None:
                    self._queue.task_done()
                    if batch:
                        await self._flush_batch(batch)
                        batch = []
                    return
                batch.append(next_item)
                self._queue.task_done()

            if len(batch) >= self._batch_size:
                await self._flush_batch(batch)
                batch = []

        if batch:
            await self._flush_batch(batch)

    async def _flush_batch(
        self, batch: list[tuple[WriteRequest, asyncio.Future[bool]]]
    ) -> None:
        loop = asyncio.get_running_loop()
        grouped: dict[str, list[tuple[WriteRequest, asyncio.Future[bool]]]] = {}
        for req, fut in batch:
            grouped.setdefault(req.op, []).append((req, fut))

        for op, items in grouped.items():
            requests = [req for req, _ in items]
            futures = [fut for _, fut in items]
            success = False
            for attempt in range(2):
                try:
                    await loop.run_in_executor(
                        self._executor, self._sync_flush, op, requests
                    )
                    success = True
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 0:
                        _log.warning("DB flush retry (attempt 1) op=%s: %s", op, exc)
                    else:
                        _log.error(
                            "DB flush FAILED op=%s count=%d: %s payload=%r",
                            op, len(requests), exc, requests,
                        )
                        self._flush_failures += len(requests)

            for fut in futures:
                if fut.done():
                    continue
                if success:
                    fut.set_result(True)
                else:
                    fut.set_exception(
                        RuntimeError(f"DB flush failed for op={op}")
                    )

        batch.clear()

    def _sync_flush(self, op: str, requests: list[WriteRequest]) -> None:
        for req in requests:
            if op == "record_fetch":
                assert isinstance(req, RecordFetchRequest)
                db_writer.record_fetch(
                    self._engine,
                    ein=req.ein,
                    url_redacted=req.url_redacted,
                    kind=req.kind,
                    fetch_status=req.fetch_status,
                    status_code=req.status_code,
                    elapsed_ms=req.elapsed_ms,
                    notes=req.notes,
                )
            elif op == "upsert_report":
                assert isinstance(req, UpsertReportRequest)
                db_writer.upsert_report(
                    self._engine,
                    content_sha256=req.content_sha256,
                    source_url_redacted=req.source_url_redacted,
                    referring_page_url_redacted=req.referring_page_url_redacted,
                    redirect_chain_redacted=req.redirect_chain_redacted,
                    source_org_ein=req.source_org_ein,
                    discovered_via=req.discovered_via,
                    hosting_platform=req.hosting_platform,
                    attribution_confidence=req.attribution_confidence,
                    content_type=req.content_type,
                    file_size_bytes=req.file_size_bytes,
                    page_count=req.page_count,
                    first_page_text=req.first_page_text,
                    pdf_creator=req.pdf_creator,
                    pdf_producer=req.pdf_producer,
                    pdf_creation_date=req.pdf_creation_date,
                    pdf_has_javascript=req.pdf_has_javascript,
                    pdf_has_launch=req.pdf_has_launch,
                    pdf_has_embedded=req.pdf_has_embedded,
                    pdf_has_uri_actions=req.pdf_has_uri_actions,
                    classification=req.classification,
                    classification_confidence=req.classification_confidence,
                    classifier_model=req.classifier_model,
                    classifier_version=req.classifier_version,
                    report_year=req.report_year,
                    report_year_source=req.report_year_source,
                    extractor_version=req.extractor_version,
                )
            elif op == "upsert_crawled_org":
                assert isinstance(req, UpsertCrawledOrgRequest)
                db_writer.upsert_crawled_org(
                    self._engine,
                    ein=req.ein,
                    candidate_count=req.candidate_count,
                    fetched_count=req.fetched_count,
                    confirmed_report_count=req.confirmed_report_count,
                )

    async def flush_and_stop(self) -> None:
        await self._queue.put(None)
        remaining: list[tuple[WriteRequest, asyncio.Future[bool]]] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                remaining.append(item)
            self._queue.task_done()
        if remaining:
            await self._flush_batch(remaining)
        self._executor.shutdown(wait=True)

    @property
    def flush_failures(self) -> int:
        return self._flush_failures


__all__ = [
    "RecordFetchRequest",
    "UpsertReportRequest",
    "UpsertCrawledOrgRequest",
    "OrgDownloadTracker",
    "DBWriterActor",
]
