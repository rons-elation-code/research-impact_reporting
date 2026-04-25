"""Main crawler orchestration (Spec 0004 + Spec 0017).

Loops the seed list from `lava_impact.nonprofits_seed`, runs
discovery → fetch → sandbox → db_writer for each candidate, and
enforces operational ACs (flock AC19, resume AC20, permissions AC21,
encryption-at-rest AC21.1).

Spec 0017: SQLite is gone. Every DB call flows through the SQLAlchemy
engine from `lavandula.common.db.make_app_engine()`. Each db_writer
call opens its own short-lived transaction via `engine.begin()`; the
connection pool handles worker-thread concurrency natively.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import fcntl
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text
from sqlalchemy.engine import Engine

from lavandula.common.db import (
    MIN_SCHEMA_VERSION,
    assert_schema_at_least,
    make_app_engine,
)

from . import config
from . import db_writer
from . import fetch_pdf
from . import archive as _archive
from . import budget
from .candidate_filter import Candidate
from .discover import per_org_candidates
from .http_client import ReportsHTTPClient, tls_self_test, TLSMisconfigured
from .logging_utils import sanitize, setup_logging
from .pdf_extract import scan_active_content, sanitize_metadata_field, sanitize_text_field
from .redirect_policy import etld1
from .robots import RobotsCache
from .url_guard import is_address_allowed
from .url_redact import redact_url
from .year_extract import infer_report_year


log = logging.getLogger("lavandula.reports.crawler")


def _iso_utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------
# Seed URL validation (AC12.4)
# ---------------------------------------------------------------------

@dataclasses.dataclass
class SeedCheck:
    ok: bool
    reason: str = ""


_BASIC_AUTH_RE = re.compile(r"@")


def validate_seed_url(url: str) -> SeedCheck:
    """AC12.4 — strict validation at the trust boundary."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return SeedCheck(ok=False, reason="parse_error")
    if parsed.scheme not in ("http", "https"):
        return SeedCheck(ok=False, reason="bad_scheme")
    netloc = parsed.netloc
    if not netloc or "@" in netloc:
        return SeedCheck(ok=False, reason="empty_host_or_userinfo")
    host = parsed.hostname or ""
    if not host:
        return SeedCheck(ok=False, reason="empty_host")

    import ipaddress
    try:
        ipaddress.ip_address(host)
        return SeedCheck(ok=False, reason="bare_ip")
    except ValueError:
        pass

    if "." not in host:
        return SeedCheck(ok=False, reason="bare_hostname")

    if host.lower() in ("localhost", "localhost.localdomain"):
        return SeedCheck(ok=False, reason="localhost")
    return SeedCheck(ok=True)


# ---------------------------------------------------------------------
# Flock (AC19)
# ---------------------------------------------------------------------

class FlockBusy(RuntimeError):
    """Another crawler instance holds the flock."""


def acquire_flock(lock_path: Path) -> int:
    """Acquire an exclusive non-blocking flock on `lock_path`. Returns fd."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise FlockBusy(f"another instance holds {lock_path}") from exc
    return fd


# ---------------------------------------------------------------------
# Resume / skip (AC20)
# ---------------------------------------------------------------------

def should_skip_ein(engine: Engine, *, ein: str, refresh: bool) -> bool:
    """Skip orgs that are settled (successful or permanently given up on).

    Status semantics (Spec 0021 + follow-up):
      - 'ok'             — already crawled successfully → skip
      - 'permanent_skip' — explicit permanent failure or N transient retries
                           exhausted → skip
      - 'transient'      — temporary failure recorded; will be retried this run
    """
    if refresh:
        return False
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM lava_impact.crawled_orgs "
                 "WHERE ein = :ein "
                 "  AND status IN ('ok', 'permanent_skip')"),
            {"ein": ein},
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------
# Encryption-at-rest (AC21.1)
# ---------------------------------------------------------------------

@dataclasses.dataclass
class EncryptionCheckResult:
    ok: bool
    reason: str = ""
    mechanism: str = ""


def check_encryption_at_rest(path: Path) -> EncryptionCheckResult:
    """AC21.1 — halt at startup if data/raw paths aren't on encrypted storage."""
    path = Path(path)
    marker = path / ".encrypted-volume"
    if marker.exists():
        return EncryptionCheckResult(
            ok=True,
            reason="operator_attested",
            mechanism="marker_file",
        )
    try:
        mounts = Path("/proc/mounts").read_text()
        for line in mounts.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            src, mnt = parts[0], parts[1]
            if any(s in src for s in ("dm-crypt", "mapper/", "ecryptfs")):
                if str(path.resolve()).startswith(mnt):
                    return EncryptionCheckResult(
                        ok=True, reason="dm-crypt", mechanism=src
                    )
    except OSError:
        pass
    return EncryptionCheckResult(
        ok=False,
        reason=(
            "no encryption auto-detected and no .encrypted-volume marker; "
            "see HANDOFF.md for marker format"
        ),
    )


# ---------------------------------------------------------------------
# HALT files
# ---------------------------------------------------------------------

def write_halt(halt_dir: Path, slug: str, body: str) -> Path:
    halt_dir = Path(halt_dir)
    halt_dir.mkdir(parents=True, exist_ok=True)
    path = halt_dir / f"HALT-{slug}.md"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------

@dataclasses.dataclass
class OrgResult:
    ein: str
    candidate_count: int = 0
    fetched_count: int = 0
    confirmed_report_count: int = 0


def _pick_discovered_via(c: Candidate) -> str:
    # Defense in depth: align with async_crawler. Sync crawler doesn't
    # produce wayback candidates today, but parity prevents future surprise.
    if (
        c.hosting_platform
        and c.hosting_platform not in ("own-domain", "wayback")
    ):
        return "hosting-platform"
    return c.discovered_via


# Per-thread HTTP client storage (TICK-002 round-2).
_thread_local = threading.local()
_thread_clients: list[ReportsHTTPClient] = []
_thread_clients_lock = threading.Lock()


def _get_thread_client() -> ReportsHTTPClient:
    c = getattr(_thread_local, "client", None)
    if c is None:
        c = ReportsHTTPClient()
        _thread_local.client = c
        with _thread_clients_lock:
            _thread_clients.append(c)
    return c


def _close_thread_clients() -> None:
    with _thread_clients_lock:
        for c in _thread_clients:
            try:
                c.session.close()
            except Exception:  # noqa: BLE001
                pass
        _thread_clients.clear()


def process_org(
    *,
    ein: str,
    website: str,
    engine: Engine,
    archive: "object | None" = None,
    archive_dir: Path | None = None,
    run_id: str = "",
    client: ReportsHTTPClient | None = None,
) -> OrgResult:
    """Process a single org end-to-end.

    All DB writes go through `db_writer.*(engine, ...)` — each call
    opens its own short-lived transaction from the engine's connection
    pool. Multiple worker threads call concurrently; the pool
    serializes connection checkout but Postgres handles write
    concurrency natively via MVCC.
    """
    if client is None:
        client = _get_thread_client()

    if archive is None:
        if archive_dir is None:
            raise ValueError("process_org requires archive or archive_dir")
        archive = _archive.LocalArchive(archive_dir)

    def _write_fetch(**kwargs):
        db_writer.record_fetch(engine, **kwargs)

    result = OrgResult(ein=ein)
    seed_etld1 = etld1(urlsplit(website).hostname or "")

    # robots
    robots_text = ""
    try:
        r = client.get(
            f"https://{urlsplit(website).hostname}/robots.txt",
            kind="robots",
            seed_etld1=seed_etld1,
        )
        if r.status == "ok" and r.body:
            robots_text = r.body.decode("utf-8", errors="replace")
        _write_fetch(
            ein=ein,
            url_redacted=r.final_url_redacted or website,
            kind="robots",
            fetch_status=r.status,
            status_code=r.http_status,
            elapsed_ms=r.elapsed_ms,
            notes=sanitize(r.note),
        )
    except Exception as exc:
        log.warning("robots fetch failed for %s: %s", ein, exc)

    def _fetcher(url: str, kind: str) -> tuple[bytes, str]:
        import time as _time
        r = None
        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            r = client.get(url, kind=kind, seed_etld1=seed_etld1)
            _write_fetch(
                ein=ein,
                url_redacted=r.final_url_redacted or redact_url(url),
                kind=kind, fetch_status=r.status, status_code=r.http_status,
                elapsed_ms=r.elapsed_ms, notes=sanitize(r.note),
            )
            retryable = (
                kind in config.RETRY_KINDS
                and r.status in config.RETRY_STATUSES
            )
            if not retryable:
                break
            if attempt < config.RETRY_MAX_ATTEMPTS - 1:
                backoff_idx = min(attempt, len(config.RETRY_BACKOFF_SEC) - 1)
                _time.sleep(config.RETRY_BACKOFF_SEC[backoff_idx])
        return (r.body or b""), r.status

    candidates = per_org_candidates(
        seed_url=website,
        seed_etld1=seed_etld1,
        fetcher=_fetcher,
        robots_text=robots_text,
        ein=ein,
    )
    result.candidate_count = len(candidates)

    for cand in candidates:
        outcome = fetch_pdf.download(
            cand.url, client, seed_etld1=seed_etld1, validate_structure=True
        )
        if outcome.status != "ok" or not outcome.body:
            _write_fetch(
                ein=ein,
                url_redacted=outcome.final_url_redacted or redact_url(cand.url),
                kind="pdf-get",
                fetch_status=outcome.status,
                status_code=None,
                elapsed_ms=None,
                notes=sanitize(outcome.note),
            )
            continue

        flags = scan_active_content(outcome.body)

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

        archive_metadata = {
            "source-url": outcome.final_url or cand.url,
            "ein": ein,
            "crawl-run-id": run_id,
            "fetched-at": _iso_utc_now(),
            "attribution-confidence": cand.attribution_confidence,
            "discovered-via": _pick_discovered_via(cand),
        }
        try:
            archive.put(
                outcome.content_sha256,
                outcome.body,
                archive_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "archive put failed sha=%s cls=%s: %s",
                outcome.content_sha256, type(exc).__name__, exc,
            )
            _write_fetch(
                ein=ein,
                url_redacted=outcome.final_url_redacted or redact_url(cand.url),
                kind="pdf-get",
                fetch_status="server_error",
                notes=sanitize(
                    f"archive_put_failed:{type(exc).__name__} "
                    f"sha={outcome.content_sha256}"
                ),
            )
            continue

        result.fetched_count += 1

        report_year, report_year_source = infer_report_year(
            source_url=outcome.final_url or cand.url,
            first_page_text=first_page_text or None,
            pdf_creation_date=str(creation_date) if creation_date else None,
        )

        db_writer.upsert_report(
            engine,
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
            classification=None,
            classification_confidence=None,
            classifier_model=config.CLASSIFIER_MODEL,
            classifier_version=config.CLASSIFIER_VERSION,
            report_year=report_year,
            report_year_source=report_year_source,
            extractor_version=config.EXTRACTOR_VERSION,
        )

        _write_fetch(
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="pdf-get",
            fetch_status=outcome.status,
            status_code=None,
            elapsed_ms=None,
            notes=sanitize(outcome.note),
        )
        _write_fetch(
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="extract",
            fetch_status=extract_status,
            notes=extract_note or (
                f"page_count={page_count}" if page_count is not None
                else "no_pages_extracted"
            ),
        )

    db_writer.upsert_crawled_org(
        engine,
        ein=ein,
        candidate_count=result.candidate_count,
        fetched_count=result.fetched_count,
        confirmed_report_count=result.confirmed_report_count,
    )
    return result


def _resolve_archive(parser: argparse.ArgumentParser, args) -> object:
    """Validate --archive / --archive-dir and construct a backend."""
    from . import s3_archive as _s3a

    archive = args.archive
    archive_dir = args.archive_dir

    if archive and archive_dir:
        parser.error("use --archive or --archive-dir, not both")
    if not archive and not archive_dir:
        parser.error("archive destination is required")

    if archive_dir:
        if str(archive_dir).startswith("s3://"):
            parser.error(
                "--archive-dir accepts only a filesystem path; "
                "use --archive for S3"
            )
        value = str(archive_dir)
    else:
        value = str(archive)

    if value.startswith("s3://"):
        try:
            bucket, prefix = _s3a.parse_s3_uri(value)
        except ValueError as exc:
            parser.error(str(exc))
        if not prefix:
            prefix = config.DEFAULT_S3_PREFIX
        elif prefix != config.DEFAULT_S3_PREFIX:
            log.warning(
                'non-standard S3 prefix "%s"; production convention is "%s"',
                prefix, config.DEFAULT_S3_PREFIX,
            )
        return _s3a.S3Archive(bucket, prefix, region=args.s3_region)

    p = Path(value)
    if not p.is_absolute():
        parser.error("archive value must be s3://... or an absolute path")
    return _archive.LocalArchive(p)


def fetch_seeds(engine: Engine) -> list[tuple[str, str]]:
    """Read (ein, website_url) pairs from `lava_impact.nonprofits_seed`."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT ein, website_url FROM lava_impact.nonprofits "
            " WHERE website_url IS NOT NULL AND website_url <> '' "
            "   AND (resolver_status IS NULL "
            "        OR resolver_status IN ('resolved', 'accepted'))"
        )).fetchall()
    return [(r[0], r[1]) for r in rows]


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spec 0004 report crawler")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=config.DATA)
    parser.add_argument(
        "--archive",
        default=None,
        help="Archive destination: s3://bucket/prefix or absolute path",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="[legacy] alias for --archive; accepts only filesystem paths",
    )
    parser.add_argument("--s3-region", default=None,
                        help="Override AWS region for the S3 archive")
    parser.add_argument("--skip-tls-self-test", action="store_true",
                        help="(ops only) skip startup TLS self-test")
    parser.add_argument("--skip-encryption-check", action="store_true",
                        help="(ops only) skip encryption-at-rest check")
    parser.add_argument("--ein", type=str, default=None,
                        help="Crawl a single org by EIN (for debugging)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max orgs to crawl (0 = no limit)")
    parser.add_argument("--max-workers", type=int, default=8,
                        help="Per-org parallelism (TICK-002). Min 1, max 32. "
                             "Default 8. Use 1 for deterministic serial runs.")
    parser.add_argument("--async", dest="use_async", action="store_true",
                        help="Use async I/O pipeline (spec 0021)")
    parser.add_argument("--max-concurrent-orgs", type=int, default=200,
                        help="(async only) Max concurrent org workers. Default 200.")
    parser.add_argument("--max-download-workers", type=int, default=20,
                        help="(async only) Max concurrent download workers. Default 20.")
    parser.add_argument("--no-wayback", action="store_true",
                        help="Disable Wayback CDX fallback (spec 0022 kill-switch)")
    args = parser.parse_args(argv)

    if args.use_async and args.max_workers != 8:
        parser.error("--async is incompatible with --max-workers")

    if args.max_workers < 1 or args.max_workers > 32:
        parser.error("--max-workers must be between 1 and 32")

    archive = _resolve_archive(parser, args)
    run_id = uuid.uuid4().hex

    logger = setup_logging(config.LOGS, name="reports-crawler")

    logger.info("=== CRAWLER START === run_id=%s limit=%s workers=%d",
                run_id, args.limit or "unlimited", args.max_workers)

    try:
        archive.startup_probe()
    except Exception as exc:
        logger.error("archive startup probe failed: %s", exc)
        return 2

    try:
        lock_fd = acquire_flock(config.LOCK_PATH)
    except FlockBusy:
        logger.error("=== CRAWLER ABORT === another instance holds the lock; exit 3")
        return 3

    try:
        if not args.skip_encryption_check:
            enc = check_encryption_at_rest(args.data_dir)
            if not enc.ok:
                write_halt(
                    config.HALT,
                    "encryption-not-detected",
                    "# HALT: data volume encryption not detected\n\n"
                    f"{enc.reason}\n",
                )
                logger.error("encryption-at-rest halt: %s", enc.reason)
                return 2

        if not args.skip_tls_self_test:
            try:
                tls_self_test()
            except TLSMisconfigured as exc:
                write_halt(
                    config.HALT,
                    "tls-misconfigured",
                    f"# HALT: TLS verification disabled\n\n{exc}\n",
                )
                logger.error("tls self-test failed: %s", exc)
                return 2

        engine = make_app_engine()
        assert_schema_at_least(engine, MIN_SCHEMA_VERSION)

        try:
            reclaimed = budget.reconcile_stale_reservations(engine)
            if reclaimed:
                logger.info("reconciled %d stale classifier preflights", reclaimed)

            if args.ein:
                seeds = [(args.ein, url) for (e, url) in fetch_seeds(engine) if e == args.ein]
                if not seeds:
                    logger.error("EIN %s not found in seeds or has no website", args.ein)
                    return 1
                logger.info("single-org mode: ein=%s url=%s", args.ein, seeds[0][1])
            else:
                seeds = fetch_seeds(engine)

            validated: list[tuple[str, str]] = []
            for ein, website in seeds:
                check = validate_seed_url(website)
                if not check.ok:
                    logger.warning("skip ein=%s bad seed %r: %s",
                                   ein, website, check.reason)
                    continue
                validated.append((ein, website))

            pending: list[tuple[str, str]] = []
            seen_eins: set[str] = set()
            for ein, website in validated:
                if ein in seen_eins:
                    continue
                seen_eins.add(ein)
                if not args.ein and should_skip_ein(engine, ein=ein, refresh=args.refresh):
                    continue
                pending.append((ein, website))

            if args.limit > 0:
                pending = pending[:args.limit]

            logger.info("crawl_plan validated=%d pending=%d (skipped=%d already-crawled or limit-capped)",
                        len(validated), len(pending), len(validated) - len(pending))

            if not pending:
                logger.info("=== CRAWLER DONE === nothing to crawl")
                return 0

            if args.no_wayback:
                config.WAYBACK_ENABLED = False

            if args.use_async:
                import asyncio as _asyncio
                from .async_crawler import run_async

                logger.info(
                    "=== ASYNC CRAWLER START === run_id=%s orgs=%d "
                    "concurrent_orgs=%d download_workers=%d",
                    run_id, len(pending),
                    args.max_concurrent_orgs, args.max_download_workers,
                )
                crawl_stats = _asyncio.run(run_async(
                    engine,
                    archive,
                    pending,
                    max_concurrent_orgs=args.max_concurrent_orgs,
                    max_download_workers=args.max_download_workers,
                    run_id=run_id,
                ))
                logger.info(
                    "=== ASYNC CRAWLER DONE === run_id=%s orgs=%d "
                    "completed=%d transient=%d permanent=%d pdfs=%d exit_code=%d",
                    run_id, len(pending),
                    crawl_stats.orgs_completed,
                    crawl_stats.orgs_transient_failed,
                    crawl_stats.orgs_permanent_failed,
                    crawl_stats.pdfs_downloaded,
                    crawl_stats.exit_code,
                )
                return crawl_stats.exit_code
            else:
                succeeded = 0
                failed = 0
                try:
                    with ThreadPoolExecutor(
                        max_workers=args.max_workers,
                        thread_name_prefix="lavandula-crawler",
                    ) as pool:
                        futures = {
                            pool.submit(
                                process_org,
                                ein=ein,
                                website=website,
                                engine=engine,
                                archive=archive,
                                run_id=run_id,
                            ): (ein, website)
                            for ein, website in pending
                        }
                        for fut in as_completed(futures):
                            ein, website = futures[fut]
                            try:
                                fut.result()
                                succeeded += 1
                            except Exception as exc:  # noqa: BLE001
                                failed += 1
                                logger.exception("ein=%s failed: %s", ein, exc)
                finally:
                    _close_thread_clients()

                logger.info("=== CRAWLER DONE === run_id=%s orgs=%d succeeded=%d failed=%d",
                            run_id, len(pending), succeeded, failed)
        finally:
            engine.dispose()

        return 0
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


__all__ = [
    "SeedCheck",
    "validate_seed_url",
    "FlockBusy",
    "acquire_flock",
    "should_skip_ein",
    "EncryptionCheckResult",
    "check_encryption_at_rest",
    "write_halt",
    "process_org",
    "fetch_seeds",
    "run",
]


if __name__ == "__main__":
    sys.exit(run())
