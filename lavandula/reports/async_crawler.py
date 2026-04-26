"""Async crawler orchestrator (Spec 0021, AC15-AC43).

Producer-consumer pipeline: org producer -> org workers -> download queue
-> download workers -> DB writer actor. Graceful shutdown on
SIGINT/SIGTERM/halt-file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy.engine import Engine

import aiohttp

from . import config
from . import archive as _archive
from . import db_writer
from .classify import classify_first_page_v2, ClassifierError
from .classifier_clients import select_classifier_client
from .taxonomy import ensure_loaded as _ensure_taxonomy, get_taxonomy, build_taxonomy_prompt_section
from .async_db_writer import (
    DBWriterActor,
    OrgDownloadTracker,
    RecordFetchRequest,
    UpsertCrawledOrgRequest,
    UpsertReportRequest,
)
from .async_discover import DiscoveryResult, discover_org
from .wayback_fallback import WaybackOutcome
from .async_fetch_pdf import download as async_download
from .async_http_client import AsyncHTTPClient
from .async_host_pin_cache import AsyncHostPinCache
from .async_host_throttle import AsyncHostThrottle
from .candidate_filter import Candidate
from .decisions_log import log_decision
from .logging_utils import sanitize
from .pdf_extract import scan_active_content, sanitize_metadata_field, sanitize_text_field
from .redirect_policy import etld1
from .url_redact import redact_url
from .year_extract import infer_report_year

_log = logging.getLogger(__name__)


@dataclass
class CrawlStats:
    orgs_total: int = 0
    orgs_completed: int = 0
    orgs_active: int = 0
    orgs_transient_failed: int = 0
    orgs_permanent_failed: int = 0
    candidates_discovered: int = 0
    pdfs_downloaded: int = 0
    download_queue_depth: int = 0
    bytes_downloaded: int = 0
    errors_by_type: dict[str, int] = field(default_factory=dict)
    start_time: float = 0.0
    flush_failures: int = 0
    exit_code: int = 0
    wayback_attempts: int = 0
    wayback_recoveries: int = 0
    wayback_empty: int = 0
    wayback_errors: int = 0


def _pick_discovered_via(c: Candidate) -> str:
    # Wayback candidates carry hosting_platform='wayback' as provenance, but
    # their discovered_via='wayback' is the load-bearing attribution. Don't
    # let the hosting-platform override clobber it (Spec 0022 AC11).
    if (
        c.hosting_platform
        and c.hosting_platform not in ("own-domain", "wayback")
    ):
        return "hosting-platform"
    return c.discovered_via


_TRANSIENT_EXCEPTIONS = (
    aiohttp.ClientConnectorError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


def _is_transient(exc: Exception) -> bool:
    return isinstance(exc, _TRANSIENT_EXCEPTIONS)


async def _org_producer(
    seeds: list[tuple[str, str]],
    org_queue: asyncio.Queue,
    shutdown_event: asyncio.Event,
    max_concurrent_orgs: int,
) -> None:
    for ein, website in seeds:
        if shutdown_event.is_set():
            break
        await org_queue.put((ein, website))
    for _ in range(max_concurrent_orgs):
        await org_queue.put(None)


async def _org_worker(
    org_queue: asyncio.Queue,
    download_queue: asyncio.Queue,
    client: AsyncHTTPClient,
    db_actor: DBWriterActor,
    archive: object,
    run_id: str,
    stats: CrawlStats,
    shutdown_event: asyncio.Event,
    pdf_thread_pool: ThreadPoolExecutor,
) -> None:
    while True:
        item = await org_queue.get()
        if item is None:
            org_queue.task_done()
            break
        ein, website = item
        stats.orgs_active += 1
        try:
            await _process_org_async(
                ein=ein,
                website=website,
                client=client,
                db_actor=db_actor,
                download_queue=download_queue,
                archive=archive,
                run_id=run_id,
                stats=stats,
                shutdown_event=shutdown_event,
                pdf_thread_pool=pdf_thread_pool,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err_key = type(exc).__name__
            stats.errors_by_type[err_key] = stats.errors_by_type.get(err_key, 0) + 1
            if _is_transient(exc):
                _log.warning("transient failure ein=%s: %s", ein, exc)
                stats.orgs_transient_failed += 1
                status = "transient"
            else:
                _log.exception("permanent failure ein=%s", ein)
                stats.orgs_permanent_failed += 1
                status = "permanent_skip"
            try:
                await db_actor.enqueue(UpsertCrawledOrgRequest(
                    ein=ein,
                    candidate_count=0,
                    fetched_count=0,
                    confirmed_report_count=0,
                    status=status,
                    run_id=run_id,
                ))
            except Exception:  # noqa: BLE001
                _log.warning(
                    "failed to record %s attempt for ein=%s", status, ein,
                )
        finally:
            stats.orgs_active -= 1
            org_queue.task_done()


async def _process_org_async(
    *,
    ein: str,
    website: str,
    client: AsyncHTTPClient,
    db_actor: DBWriterActor,
    download_queue: asyncio.Queue,
    archive: object,
    run_id: str,
    stats: CrawlStats,
    shutdown_event: asyncio.Event,
    pdf_thread_pool: ThreadPoolExecutor,
) -> None:
    seed_etld1 = etld1(urlsplit(website).hostname or "")

    robots_text = ""
    try:
        r = await client.get(
            f"https://{urlsplit(website).hostname}/robots.txt",
            kind="robots",
            seed_etld1=seed_etld1,
        )
        if r.status == "ok" and r.body:
            robots_text = r.body.decode("utf-8", errors="replace")
        await db_actor.enqueue(RecordFetchRequest(
            ein=ein,
            url_redacted=r.final_url_redacted or website,
            kind="robots",
            fetch_status=r.status,
            status_code=r.http_status,
            elapsed_ms=r.elapsed_ms,
            notes=sanitize(r.note),
        ))
    except Exception as exc:
        _log.warning("robots fetch failed for %s: %s", ein, exc)

    async def _fetcher_with_retry(url: str, kind: str) -> tuple[bytes, str]:
        r = None
        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            r = await client.get(url, kind=kind, seed_etld1=seed_etld1)
            await db_actor.enqueue(RecordFetchRequest(
                ein=ein,
                url_redacted=r.final_url_redacted or redact_url(url),
                kind=kind,
                fetch_status=r.status,
                status_code=r.http_status,
                elapsed_ms=r.elapsed_ms,
                notes=sanitize(r.note),
            ))
            retryable = kind in config.RETRY_KINDS and r.status in config.RETRY_STATUSES
            if not retryable:
                break
            if attempt < config.RETRY_MAX_ATTEMPTS - 1:
                backoff_idx = min(attempt, len(config.RETRY_BACKOFF_SEC) - 1)
                await asyncio.sleep(config.RETRY_BACKOFF_SEC[backoff_idx])
        return (r.body or b""), r.status

    org_t0 = time.monotonic()
    discovery_t0 = time.monotonic()
    discovery = await discover_org(
        seed_url=website,
        seed_etld1=seed_etld1,
        client=client,
        robots_text=robots_text,
        ein=ein,
        fetcher=_fetcher_with_retry,
        stats=stats,
    )
    discovery_ms = int((time.monotonic() - discovery_t0) * 1000)
    candidates = discovery.candidates
    stats.candidates_discovered += len(candidates)

    if not candidates and not discovery.homepage_ok and not discovery.robots_disallowed_all:
        if discovery.wayback_outcome is not None:
            await _record_no_candidates(
                ein=ein,
                domain=urlsplit(website).hostname or seed_etld1,
                db_actor=db_actor,
                stats=stats,
                discovery=discovery,
                run_id=run_id,
            )
        else:
            _log.warning("transient discovery failure ein=%s (homepage unreachable, 0 candidates)", ein)
            stats.orgs_transient_failed += 1
            try:
                await db_actor.enqueue(UpsertCrawledOrgRequest(
                    ein=ein,
                    candidate_count=0,
                    fetched_count=0,
                    confirmed_report_count=0,
                    status="transient",
                    run_id=run_id,
                ))
            except Exception:  # noqa: BLE001
                _log.warning("failed to record transient attempt for ein=%s", ein)
        return

    org_tracker = OrgDownloadTracker()
    org_fetched = [0]
    org_active_content_rejections = [0]

    download_t0 = time.monotonic()
    for cand in candidates:
        if shutdown_event.is_set():
            break
        org_tracker.increment()
        await download_queue.put((ein, cand, org_tracker, seed_etld1, org_fetched, org_active_content_rejections))

    await org_tracker.wait_all_done()
    download_ms = int((time.monotonic() - download_t0) * 1000)
    total_ms = int((time.monotonic() - org_t0) * 1000)

    # Wayback post-download outcome tracking (AC18/AC19)
    is_wayback_recovery = (
        discovery.wayback_outcome == WaybackOutcome.RECOVERED
        and any(c.discovered_via == "wayback" for c in candidates)
    )
    if is_wayback_recovery and org_fetched[0] == 0:
        all_active_content = (
            org_active_content_rejections[0] > 0
            and org_active_content_rejections[0] == len(candidates)
        )
        if all_active_content:
            status = "permanent_skip"
            notes = "wayback_unsafe_archive"
            outcome_label = "unsafe_archive"
            stats.orgs_permanent_failed += 1
        else:
            status = "transient"
            notes = "wayback_all_downloads_failed"
            outcome_label = "all_downloads_failed"
            stats.orgs_transient_failed += 1
        stats.wayback_errors += 1
        try:
            await db_actor.enqueue(UpsertCrawledOrgRequest(
                ein=ein,
                candidate_count=len(candidates),
                fetched_count=0,
                confirmed_report_count=0,
                status=status,
                notes=sanitize(notes),
                run_id=run_id,
            ))
        except Exception:  # noqa: BLE001
            _log.warning("failed to record wayback %s for ein=%s", notes, ein)
        _log_wayback_decision(
            ein=ein,
            domain=urlsplit(website).hostname or seed_etld1,
            outcome=outcome_label,
            discovery=discovery,
        )
        return

    if is_wayback_recovery and org_fetched[0] > 0:
        stats.wayback_recoveries += 1
        _log_wayback_decision(
            ein=ein,
            domain=urlsplit(website).hostname or seed_etld1,
            outcome="recovered",
            discovery=discovery,
        )

    completion_future = await db_actor.enqueue(UpsertCrawledOrgRequest(
        ein=ein,
        candidate_count=len(candidates),
        fetched_count=org_fetched[0],
        confirmed_report_count=0,
        run_id=run_id,
        discovery_ms=discovery_ms,
        download_ms=download_ms,
        total_ms=total_ms,
    ))
    try:
        await completion_future
    except Exception:  # noqa: BLE001
        _log.warning("org completion flush failed for ein=%s", ein)
    stats.orgs_completed += 1


async def _download_worker(
    download_queue: asyncio.Queue,
    client: AsyncHTTPClient,
    db_actor: DBWriterActor,
    archive: object,
    run_id: str,
    stats: CrawlStats,
    pdf_thread_pool: ThreadPoolExecutor,
    classifier_client: object | None = None,
) -> None:
    while True:
        item = await download_queue.get()
        if item is None:
            download_queue.task_done()
            break
        ein, cand, org_tracker, seed_etld1, org_fetched, org_active_content_rej = item
        try:
            await _process_download(
                ein=ein,
                cand=cand,
                client=client,
                db_actor=db_actor,
                archive=archive,
                run_id=run_id,
                seed_etld1=seed_etld1,
                stats=stats,
                pdf_thread_pool=pdf_thread_pool,
                org_fetched=org_fetched,
                org_active_content_rejections=org_active_content_rej,
                classifier_client=classifier_client,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning("download failed ein=%s url=%s: %s", ein, cand.url, exc)
        finally:
            org_tracker.decrement()
            download_queue.task_done()


async def _process_download(
    *,
    ein: str,
    cand: Candidate,
    client: AsyncHTTPClient,
    db_actor: DBWriterActor,
    archive: object,
    run_id: str,
    seed_etld1: str,
    stats: CrawlStats,
    pdf_thread_pool: ThreadPoolExecutor,
    org_fetched: list[int] | None = None,
    org_active_content_rejections: list[int] | None = None,
    classifier_client: object | None = None,
) -> None:
    outcome = await async_download(
        cand.url, client, seed_etld1=seed_etld1,
        validate_structure=True, thread_pool=pdf_thread_pool,
    )

    if outcome.status != "ok" or not outcome.body:
        await db_actor.enqueue(RecordFetchRequest(
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="pdf-get",
            fetch_status=outcome.status,
            notes=sanitize(outcome.note),
        ))
        return

    flags = scan_active_content(outcome.body)

    # AC17.1: Wayback PDFs with active content are rejected outright
    if cand.discovered_via == "wayback" and (
        flags["pdf_has_javascript"]
        or flags["pdf_has_launch"]
        or flags["pdf_has_uri_actions"]
    ):
        if org_active_content_rejections is not None:
            org_active_content_rejections[0] += 1
        await db_actor.enqueue(RecordFetchRequest(
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="pdf-get",
            fetch_status="blocked_content_type",
            notes=sanitize("wayback_active_content"),
        ))
        return

    first_page_text = ""
    page_count = None
    creator = None
    producer = None
    creation_date = None
    extract_status = "ok"
    extract_note = ""
    try:
        import io as _io
        from pypdf import PdfReader as _PdfReader
        reader = _PdfReader(_io.BytesIO(outcome.body))
        page_count = len(reader.pages)
        if page_count:
            first_page_text = (
                sanitize_text_field(reader.pages[0].extract_text()) or ""
            )[:4096]
        meta = reader.metadata or {}
        creator = meta.get("/Creator") if isinstance(meta, dict) else getattr(meta, "creator", None)
        producer = meta.get("/Producer") if isinstance(meta, dict) else getattr(meta, "producer", None)
        creation_date = (
            meta.get("/CreationDate") if isinstance(meta, dict)
            else getattr(meta, "creation_date_raw", None)
        )
    except Exception as exc:  # noqa: BLE001
        extract_status = "server_error"
        extract_note = sanitize(str(exc))

    import datetime
    archive_metadata = {
        "source-url": outcome.final_url or cand.url,
        "ein": ein,
        "crawl-run-id": run_id,
        "fetched-at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "attribution-confidence": cand.attribution_confidence,
        "discovered-via": _pick_discovered_via(cand),
    }
    try:
        archive.put(outcome.content_sha256, outcome.body, archive_metadata)
    except Exception as exc:  # noqa: BLE001
        _log.warning("archive put failed sha=%s: %s", outcome.content_sha256, exc)
        await db_actor.enqueue(RecordFetchRequest(
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="pdf-get",
            fetch_status="server_error",
            notes=sanitize(f"archive_put_failed:{type(exc).__name__}"),
        ))
        return

    stats.pdfs_downloaded += 1
    stats.bytes_downloaded += len(outcome.body)
    if org_fetched is not None:
        org_fetched[0] += 1

    report_year, report_year_source = infer_report_year(
        source_url=outcome.final_url or cand.url,
        first_page_text=first_page_text or None,
        pdf_creation_date=str(creation_date) if creation_date else None,
    )

    cls_result = None
    if first_page_text and classifier_client is not None:
        loop = asyncio.get_running_loop()
        taxonomy = get_taxonomy()
        try:
            cls_result = await loop.run_in_executor(
                pdf_thread_pool,
                lambda: classify_first_page_v2(
                    first_page_text,
                    client=classifier_client,
                    taxonomy=taxonomy,
                    raise_on_error=False,
                ),
            )
            if cls_result.classification is None:
                cls_result = None
        except Exception:  # noqa: BLE001
            cls_result = None

    report_future = await db_actor.enqueue(UpsertReportRequest(
        content_sha256=outcome.content_sha256,
        source_url_redacted=outcome.final_url_redacted or redact_url(cand.url),
        referring_page_url_redacted=redact_url(cand.referring_page_url),
        redirect_chain_redacted=outcome.redirect_chain_redacted,
        source_org_ein=ein,
        discovered_via=_pick_discovered_via(cand),
        hosting_platform=cand.hosting_platform,
        attribution_confidence=cand.attribution_confidence,
        file_size_bytes=len(outcome.body),
        page_count=page_count,
        first_page_text=first_page_text or None,
        pdf_creator=sanitize_metadata_field(str(creator) if creator else None),
        pdf_producer=sanitize_metadata_field(str(producer) if producer else None),
        pdf_creation_date=sanitize_metadata_field(str(creation_date) if creation_date else None),
        pdf_has_javascript=flags["pdf_has_javascript"],
        pdf_has_launch=flags["pdf_has_launch"],
        pdf_has_embedded=flags["pdf_has_embedded"],
        pdf_has_uri_actions=flags["pdf_has_uri_actions"],
        classification=cls_result.classification if cls_result else None,
        classification_confidence=cls_result.classification_confidence if cls_result else None,
        classifier_model=cls_result.classifier_model if cls_result else config.CLASSIFIER_MODEL,
        classifier_version=2 if cls_result else config.CLASSIFIER_VERSION,
        report_year=report_year,
        report_year_source=report_year_source,
        extractor_version=config.EXTRACTOR_VERSION,
        original_source_url_redacted=(
            redact_url(cand.original_source_url)
            if cand.original_source_url else None
        ),
        material_type=cls_result.material_type if cls_result else None,
        material_group=cls_result.material_group if cls_result else None,
        event_type=cls_result.event_type if cls_result else None,
        reasoning=cls_result.reasoning if cls_result else None,
        run_id=run_id,
    ))

    # AC19.1: include wayback_digest in fetch_log notes
    fetch_notes = outcome.note or ""
    if cand.wayback_digest:
        digest_note = sanitize(f"wayback_digest:{cand.wayback_digest}")
        fetch_notes = f"{fetch_notes} {digest_note}".strip() if fetch_notes else digest_note
    await db_actor.enqueue(RecordFetchRequest(
        ein=ein,
        url_redacted=outcome.final_url_redacted or redact_url(cand.url),
        kind="pdf-get",
        fetch_status=outcome.status,
        notes=sanitize(fetch_notes) if fetch_notes else None,
    ))
    await db_actor.enqueue(RecordFetchRequest(
        ein=ein,
        url_redacted=outcome.final_url_redacted or redact_url(cand.url),
        kind="extract",
        fetch_status=extract_status,
        notes=extract_note or (
            f"page_count={page_count}" if page_count is not None
            else "no_pages_extracted"
        ),
    ))

    try:
        await report_future
    except Exception:  # noqa: BLE001
        _log.warning("report flush failed for sha=%s", outcome.content_sha256)


async def _record_no_candidates(
    *,
    ein: str,
    domain: str,
    db_actor: DBWriterActor,
    stats: CrawlStats,
    discovery: DiscoveryResult,
    run_id: str = "",
) -> None:
    """AC14 state machine for orgs whose discovery produced no candidates."""
    notes = "robots_or_unknown"
    log_outcome = "skipped"
    if discovery.wayback_outcome == WaybackOutcome.ERROR:
        stats.wayback_errors += 1
        notes = "wayback_error"
        log_outcome = "error"
    elif discovery.wayback_outcome == WaybackOutcome.INVALID_DOMAIN:
        stats.wayback_errors += 1
        notes = "wayback_invalid_domain"
        log_outcome = "invalid_domain"
    elif discovery.wayback_outcome == WaybackOutcome.EMPTY:
        stats.wayback_empty += 1
        notes = "wayback_no_coverage"
        log_outcome = "empty"

    stats.orgs_transient_failed += 1
    try:
        await db_actor.enqueue(UpsertCrawledOrgRequest(
            ein=ein,
            candidate_count=0,
            fetched_count=0,
            confirmed_report_count=0,
            status="transient",
            notes=sanitize(notes),
            run_id=run_id,
        ))
    except Exception:  # noqa: BLE001
        _log.warning("failed to record wayback no-candidates for ein=%s", ein)

    if discovery.wayback_outcome is not None:
        _log_wayback_decision(
            ein=ein,
            domain=domain,
            outcome=log_outcome,
            discovery=discovery,
        )


def _log_wayback_decision(
    *,
    ein: str,
    domain: str,
    outcome: str,
    discovery: DiscoveryResult,
) -> None:
    """AC19: emit a wayback_query event to decisions_log."""
    log_decision({
        "event_type": "wayback_query",
        "ein": ein,
        "domain": domain,
        "outcome": outcome,
        "reason": discovery.homepage_failure_reason or "unknown",
        "cdx_http_status": discovery.wayback_cdx_http_status,
        "row_count_raw": discovery.wayback_raw_row_count,
        "row_count_after_dedup": (
            len(discovery.candidates)
            if discovery.wayback_outcome == WaybackOutcome.RECOVERED else 0
        ),
        "elapsed_ms": discovery.wayback_elapsed_ms,
        "capture_hosts": discovery.wayback_capture_hosts,
    })


async def _halt_sentinel(
    halt_dir: Path,
    shutdown_event: asyncio.Event,
) -> None:
    while not shutdown_event.is_set():
        try:
            if any(halt_dir.glob("HALT-*.md")):
                _log.info("halt file detected in %s", halt_dir)
                shutdown_event.set()
                return
        except OSError:
            pass
        await asyncio.sleep(30)


async def _progress_reporter(
    stats: CrawlStats,
    shutdown_event: asyncio.Event,
    download_queue: asyncio.Queue | None = None,
) -> None:
    while not shutdown_event.is_set():
        await asyncio.sleep(60)
        if download_queue is not None:
            stats.download_queue_depth = download_queue.qsize()
        elapsed = time.monotonic() - stats.start_time
        rate = stats.orgs_completed / (elapsed / 3600) if elapsed > 0 else 0
        remaining = stats.orgs_total - stats.orgs_completed
        eta_hours = remaining / rate if rate > 0 else 0
        eta_days = int(eta_hours // 24)
        eta_h = int(eta_hours % 24)
        _log.info(
            "orgs: %d/%d (%.1f%%) | active: %d | queue: %d | PDFs: %d | "
            "rate: %.0f orgs/hr | ETA: %dd %dh",
            stats.orgs_completed, stats.orgs_total,
            100 * stats.orgs_completed / max(stats.orgs_total, 1),
            stats.orgs_active, stats.download_queue_depth,
            stats.pdfs_downloaded, rate, eta_days, eta_h,
        )


def _validate_halt_dir(halt_dir: Path) -> None:
    if not halt_dir.exists():
        halt_dir.mkdir(parents=True, exist_ok=True)
    stat = halt_dir.stat()
    if stat.st_mode & 0o002:
        raise RuntimeError(
            f"halt directory {halt_dir} is world-writable — refusing to start"
        )


async def run_async(
    engine: Engine,
    archive: object,
    seeds: list[tuple[str, str]],
    *,
    max_concurrent_orgs: int = 200,
    max_download_workers: int = 20,
    run_id: str = "",
    halt_dir: Path | None = None,
) -> CrawlStats:
    _ensure_taxonomy()

    if halt_dir is None:
        halt_dir = config.HALT

    _validate_halt_dir(halt_dir)

    code_version = db_writer.git_short_sha()
    try:
        db_writer.create_run(
            engine,
            run_id=run_id,
            mode="async_crawl",
            code_version=code_version,
            config_json=json.dumps({
                "max_concurrent_orgs": max_concurrent_orgs,
                "max_download_workers": max_download_workers,
                "seed_count": len(seeds),
            }),
        )
    except Exception:  # noqa: BLE001
        _log.warning("failed to create runs row for run_id=%s", run_id)

    stats = CrawlStats(
        orgs_total=len(seeds),
        start_time=time.monotonic(),
    )

    shutdown_event = asyncio.Event()
    sigint_count = [0]
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        sigint_count[0] += 1
        if sigint_count[0] >= 2:
            _log.warning("double signal — force exit")
            os._exit(1)
        _log.info("shutdown signal received — draining")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    try:
        classifier_client = select_classifier_client()
        _log.info("classifier client: %s", type(classifier_client).__name__)
    except Exception:  # noqa: BLE001
        classifier_client = None
        _log.warning("no classifier client available — inline classification disabled")

    throttle = AsyncHostThrottle(host_overrides={
        "archive.org": config.WAYBACK_REQUEST_DELAY_SEC,
    })
    pin_cache = AsyncHostPinCache()
    pdf_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pdf-validate")

    async with AsyncHTTPClient(
        throttle=throttle, pin_cache=pin_cache
    ) as client:
        db_actor = DBWriterActor(engine)
        org_queue: asyncio.Queue = asyncio.Queue(maxsize=max_concurrent_orgs)
        download_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        db_task = asyncio.create_task(db_actor.run())
        halt_task = asyncio.create_task(_halt_sentinel(halt_dir, shutdown_event))
        reporter_task = asyncio.create_task(
            _progress_reporter(stats, shutdown_event, download_queue)
        )

        producer_task = asyncio.create_task(
            _org_producer(seeds, org_queue, shutdown_event, max_concurrent_orgs)
        )

        org_worker_tasks = [
            asyncio.create_task(
                _org_worker(
                    org_queue, download_queue, client, db_actor, archive,
                    run_id, stats, shutdown_event, pdf_thread_pool,
                )
            )
            for _ in range(max_concurrent_orgs)
        ]

        download_worker_tasks = [
            asyncio.create_task(
                _download_worker(
                    download_queue, client, db_actor, archive,
                    run_id, stats, pdf_thread_pool,
                    classifier_client=classifier_client,
                )
            )
            for _ in range(max_download_workers)
        ]

        await producer_task
        await asyncio.gather(*org_worker_tasks)

        for _ in range(max_download_workers):
            await download_queue.put(None)
        await asyncio.gather(*download_worker_tasks)

        await asyncio.shield(db_actor.flush_and_stop())
        db_task.cancel()
        try:
            await db_task
        except asyncio.CancelledError:
            pass

        halt_task.cancel()
        reporter_task.cancel()
        for t in (halt_task, reporter_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    pdf_thread_pool.shutdown(wait=False)

    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    elapsed = time.monotonic() - stats.start_time
    rate = stats.orgs_completed / (elapsed / 3600) if elapsed > 0 else 0
    _log.info(
        "=== ASYNC CRAWLER DONE === orgs=%d PDFs=%d bytes=%d "
        "wall=%.0fs rate=%.0f orgs/hr peak_rss_kb=%d "
        "wayback_attempts=%d wayback_recoveries=%d wayback_empty=%d wayback_errors=%d",
        stats.orgs_completed, stats.pdfs_downloaded,
        stats.bytes_downloaded, elapsed, rate, peak_rss_kb,
        stats.wayback_attempts, stats.wayback_recoveries,
        stats.wayback_empty, stats.wayback_errors,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.remove_signal_handler(sig)

    stats.flush_failures = db_actor.flush_failures
    stats.exit_code = 1 if db_actor.flush_failures > 0 else 0
    if stats.exit_code != 0:
        _log.warning("exit_code=1 due to %d unresolved flush failures",
                      db_actor.flush_failures)

    try:
        db_writer.finish_run(
            engine,
            run_id=run_id,
            stats_json=json.dumps({
                "orgs_completed": stats.orgs_completed,
                "orgs_transient_failed": stats.orgs_transient_failed,
                "orgs_permanent_failed": stats.orgs_permanent_failed,
                "pdfs_downloaded": stats.pdfs_downloaded,
                "bytes_downloaded": stats.bytes_downloaded,
                "wall_seconds": int(elapsed),
                "flush_failures": stats.flush_failures,
            }),
        )
    except Exception:  # noqa: BLE001
        _log.warning("failed to finish runs row for run_id=%s", run_id)

    return stats


__all__ = ["run_async", "CrawlStats"]
