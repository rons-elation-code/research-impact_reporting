"""Main crawler orchestration (Phase 6).

Loops the 0001 seed list, validates each seed URL (AC12.4), runs
discovery → fetch → sandbox → classify → db_writer for each candidate,
and enforces operational ACs (flock AC19, resume AC20, permissions
AC21, encryption-at-rest AC21.1).
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from . import db_writer
from . import schema
from . import fetch_pdf
from . import archive as _archive
from . import budget
from .candidate_filter import Candidate
from .db_queue import DBWriter, DBWriterDied
from .discover import per_org_candidates
from .http_client import ReportsHTTPClient, tls_self_test, TLSMisconfigured
from .logging_utils import sanitize, setup_logging
from .pdf_extract import scan_active_content, sanitize_metadata_field
from .redirect_policy import etld1
from .robots import RobotsCache
from .url_guard import is_address_allowed
from .url_redact import redact_url
from .year_extract import infer_report_year


log = logging.getLogger("lavandula.reports.crawler")


# ---------------------------------------------------------------------
# Seed URL validation (AC12.4)
# ---------------------------------------------------------------------

@dataclasses.dataclass
class SeedCheck:
    ok: bool
    reason: str = ""


_BASIC_AUTH_RE = re.compile(r"@")


def validate_seed_url(url: str) -> SeedCheck:
    """AC12.4 — strict validation at the trust boundary.

    Rejects:
      - non-http(s) schemes (javascript:, file:, data:, ftp:, …)
      - URLs with userinfo (user:pass@)
      - empty hostnames
      - IP-literal hosts (public OR private)
      - hosts resolving to SSRF-blocked addresses
    """
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

    # Reject bare IPs (bypass the public-suffix expectation).
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return SeedCheck(ok=False, reason="bare_ip")
    except ValueError:
        pass

    # Reject hosts that look like bare single labels (no dot) — the
    # public-suffix list check below handles the full hostname shape,
    # but catching localhost / site-local names here is simpler and
    # defends against resolver override.
    if "." not in host:
        return SeedCheck(ok=False, reason="bare_hostname")

    # SSRF guard: if the host is itself an IP, it's already rejected
    # above. For proper hostnames we defer the IP check to HostPinCache
    # at fetch time — but we still refuse known-bad hostnames like
    # 'localhost' here as belt-and-suspenders.
    if host.lower() in ("localhost", "localhost.localdomain"):
        return SeedCheck(ok=False, reason="localhost")
    return SeedCheck(ok=True)


# ---------------------------------------------------------------------
# Flock (AC19)
# ---------------------------------------------------------------------

class FlockBusy(RuntimeError):
    """Another crawler instance holds the flock."""


def acquire_flock(lock_path: Path) -> int:
    """Acquire an exclusive non-blocking flock on `lock_path`. Returns fd.

    Subsequent callers while the fd is open receive FlockBusy.
    """
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

def should_skip_ein(conn: sqlite3.Connection, *, ein: str, refresh: bool) -> bool:
    if refresh:
        return False
    row = conn.execute(
        "SELECT 1 FROM crawled_orgs WHERE ein = ?", (ein,)
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
    """AC21.1 — halt at startup if data/raw paths aren't on encrypted storage.

    Detection order (first hit wins):
      (a) /proc/mounts LUKS / dm-crypt / fscrypt flag.
      (b) macOS diskutil apfs encryption (not exercised here; placeholder).
      (c) operator-signed `.encrypted-volume` marker file in `path`.

    No detection → ok=False with a reason; caller halts with the
    HALT-encryption-not-detected.md file.
    """
    path = Path(path)
    marker = path / ".encrypted-volume"
    if marker.exists():
        return EncryptionCheckResult(
            ok=True,
            reason="operator_attested",
            mechanism="marker_file",
        )
    # /proc/mounts heuristic: look for dm-crypt / ecryptfs / fscrypt.
    try:
        mounts = Path("/proc/mounts").read_text()
        for line in mounts.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            src, mnt = parts[0], parts[1]
            if any(s in src for s in ("dm-crypt", "mapper/", "ecryptfs")):
                # Crude: if the project path starts with mnt, assume encrypted.
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
    """Write a HALT-*.md file and return its path."""
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
    # TICK-002: classification is deferred out of the crawler, so this
    # always stays 0 at crawl time. Downstream catalogue/reporting should
    # derive the real count from the `reports` table once classify_null
    # has run, not from `crawled_orgs.confirmed_report_count`.
    confirmed_report_count: int = 0


def _pick_discovered_via(c: Candidate) -> str:
    if c.hosting_platform and c.hosting_platform != "own-domain":
        return "hosting-platform"
    return c.discovered_via


# Per-thread HTTP client storage (TICK-002 round-2 review).
#
# `requests.Session` is not thread-safe, so each worker thread gets
# its own `ReportsHTTPClient`. We use `threading.local()` + a registry
# so the client is created once per thread (not once per org) and
# explicitly closed at run-end to avoid socket/TLS leaks.

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
    """Close all per-thread HTTP clients opened during the run."""
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
    archive_dir: Path,
    db_queue: "DBWriter | None" = None,
    client: ReportsHTTPClient | None = None,
    conn: sqlite3.Connection | None = None,
) -> OrgResult:
    """Process a single org end-to-end.

    TICK-002: classification is NO LONGER invoked here. Rows are written
    with `classification=NULL`; a separate `classify_null.py` pass fills
    them in afterward.

    Concurrency:
      - parallel path: `db_queue` is passed, `client` is left `None`,
        and the function fetches the per-thread cached client
        (one `ReportsHTTPClient` per worker thread, reused across
        orgs). Clients are closed at end-of-run.
      - legacy/serial path: a caller-provided `conn` + `client` are
        used directly (no queue).
    """
    if client is None:
        client = _get_thread_client()

    def _write_fetch(**kwargs):
        db_writer.record_fetch(conn, db_writer=db_queue, **kwargs)

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
    )
    result.candidate_count = len(candidates)

    for cand in candidates:
        outcome = fetch_pdf.download(
            cand.url, client, seed_etld1=seed_etld1, validate_structure=True
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
        if outcome.status != "ok" or not outcome.body:
            continue
        result.fetched_count += 1

        try:
            archive_path = _archive.write_pdf(
                outcome.body,
                outcome.content_sha256,
                archive_dir=archive_dir,
            )
        except Exception as exc:
            log.warning("archive write failed for sha=%s: %s", outcome.content_sha256, exc)
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
                first_page_text = (reader.pages[0].extract_text() or "")[:4096]
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

        _write_fetch(
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="extract",
            fetch_status=extract_status,
            notes=extract_note or (f"page_count={page_count}" if page_count is not None else "no_pages_extracted"),
        )

        report_year, report_year_source = infer_report_year(
            source_url=outcome.final_url or cand.url,
            first_page_text=first_page_text or None,
            pdf_creation_date=str(creation_date) if creation_date else None,
        )

        db_writer.upsert_report(
            conn,
            db_writer=db_queue,
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

    db_writer.upsert_crawled_org(
        conn,
        db_writer=db_queue,
        ein=ein,
        candidate_count=result.candidate_count,
        fetched_count=result.fetched_count,
        confirmed_report_count=result.confirmed_report_count,
    )
    return result


def fetch_seeds_from_0001(
    nonprofits_db: Path,
) -> list[tuple[str, str]]:
    """Read seed (ein, website) pairs from 0001's nonprofits.db."""
    conn = sqlite3.connect(str(nonprofits_db))
    try:
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT ein, website_url FROM nonprofits "
                "WHERE website_url IS NOT NULL AND website_url != '' "
                "AND (resolver_status IS NULL OR resolver_status IN ('resolved', 'accepted'))"
            )
        ]
    finally:
        conn.close()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spec 0004 report crawler")
    parser.add_argument("--nonprofits-db", type=Path, default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--retry-null-classifications", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=config.DATA)
    parser.add_argument("--archive-dir", type=Path, default=config.RAW)
    parser.add_argument("--skip-tls-self-test", action="store_true",
                        help="(ops only) skip startup TLS self-test")
    parser.add_argument("--skip-encryption-check", action="store_true",
                        help="(ops only) skip encryption-at-rest check")
    parser.add_argument("--max-workers", type=int, default=8,
                        help="Per-org parallelism (TICK-002). Min 1, max 32. "
                             "Default 8. Use 1 for deterministic serial runs.")
    args = parser.parse_args(argv)

    if args.max_workers < 1 or args.max_workers > 32:
        parser.error("--max-workers must be between 1 and 32")

    logger = setup_logging(config.LOGS, name="reports-crawler")

    # AC19 flock.
    try:
        lock_fd = acquire_flock(config.LOCK_PATH)
    except FlockBusy:
        logger.error("another crawler instance is running; exit 3")
        return 3

    try:
        # AC21.1 encryption-at-rest.
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

        # AC11 TLS self-test.
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

        db_path = args.data_dir / "reports.db"
        conn = schema.ensure_db(db_path)
        try:
            # Reconcile any crashed-mid-settle preflight reservations.
            reclaimed = budget.reconcile_stale_reservations(conn)
            if reclaimed:
                logger.info("reconciled %d stale classifier preflights", reclaimed)

            seeds: Iterable[tuple[str, str]] = []
            if args.nonprofits_db:
                seeds = fetch_seeds_from_0001(args.nonprofits_db)

            # Pre-filter seeds: skip already-crawled + invalid URLs on the
            # main thread so workers only receive work they should do.
            # Dedupe by EIN — with parallel dispatch, duplicate EINs in
            # the seed list would race into the same crawled_orgs row
            # instead of being deduped by the serial should_skip_ein
            # check. Keep the first occurrence.
            pending: list[tuple[str, str]] = []
            seen_eins: set[str] = set()
            for ein, website in seeds:
                if ein in seen_eins:
                    continue
                seen_eins.add(ein)
                if should_skip_ein(conn, ein=ein, refresh=args.refresh):
                    continue
                check = validate_seed_url(website)
                if not check.ok:
                    logger.warning("skip ein=%s bad seed %r: %s", ein, website, check.reason)
                    continue
                pending.append((ein, website))

            # Parallel dispatch via single-writer DB queue + worker pool.
            # The read-only `conn` stays on the main thread; writes flow
            # through `writer`. `max_workers=1` preserves serial behavior.
            writer = DBWriter(str(db_path))
            writer.start()
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
                            archive_dir=args.archive_dir,
                            db_queue=writer,
                        ): (ein, website)
                        for ein, website in pending
                    }
                    for fut in as_completed(futures):
                        ein, website = futures[fut]
                        if not writer.is_alive():
                            logger.error("db writer died; aborting run")
                            for f in futures:
                                f.cancel()
                            raise RuntimeError("db writer thread died")
                        try:
                            fut.result()
                        except DBWriterDied:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("ein=%s failed: %s", ein, exc)
            finally:
                writer.stop()
                _close_thread_clients()
        finally:
            conn.close()

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
    "fetch_seeds_from_0001",
    "run",
]


if __name__ == "__main__":
    sys.exit(run())
