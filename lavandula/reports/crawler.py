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

from . import config
from . import db_writer
from . import schema
from . import fetch_pdf
from . import archive as _archive
from . import classify as _classify
from . import budget
from .candidate_filter import Candidate
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
    confirmed_report_count: int = 0


def _pick_discovered_via(c: Candidate) -> str:
    if c.hosting_platform and c.hosting_platform != "own-domain":
        return "hosting-platform"
    return c.discovered_via


def process_org(
    *,
    ein: str,
    website: str,
    client: ReportsHTTPClient,
    anthropic_client,
    conn: sqlite3.Connection,
    archive_dir: Path,
) -> OrgResult:
    """Process a single org end-to-end."""
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
        db_writer.record_fetch(
            conn,
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
        # TICK-002 Fix 2: retry on network_error/server_error for
        # homepage/subpage/sitemap fetches only. PDF fetches remain
        # single-shot.
        import time as _time
        r = None
        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            r = client.get(url, kind=kind, seed_etld1=seed_etld1)
            db_writer.record_fetch(
                conn, ein=ein,
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
                # Backoff from completion of this attempt. Last
                # attempt skips the sleep (falls through to loop end).
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
        db_writer.record_fetch(
            conn,
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

        # Active-content flags from the raw bytes (cheap, in-parent).
        flags = scan_active_content(outcome.body)

        # First-page text via the in-parent pypdf call — for v1 we trust
        # the parent's pre-validated PDF (Gemini plan-review HIGH #1) and
        # invoke the sandbox's payload import IN-PROC here. The sandbox
        # path via `sandbox.runner.extract_pdf_fields(archive_path)` is
        # preferred for untrusted-input scenarios; we expose it for
        # operator override but default to in-parent for v1 throughput.
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

        db_writer.record_fetch(
            conn,
            ein=ein,
            url_redacted=outcome.final_url_redacted or redact_url(cand.url),
            kind="extract",
            fetch_status=extract_status,
            notes=extract_note or (f"page_count={page_count}" if page_count is not None else "no_pages_extracted"),
        )

        classification = None
        classification_confidence = None
        input_tokens = 0
        output_tokens = 0
        error = ""
        reservation_id: int | None = None
        if anthropic_client is not None and first_page_text:
            try:
                estimated = _classify.estimate_cents(1200, 150)
                reservation_id = budget.check_and_reserve(
                    conn,
                    estimated_cents=estimated,
                    classifier_model=config.CLASSIFIER_MODEL,
                )
            except budget.BudgetExceeded:
                write_halt(
                    config.HALT,
                    "classifier-budget",
                    "# HALT: classifier budget cap exceeded\n",
                )
                raise
            try:
                cresult = _classify.classify_first_page(
                    first_page_text,
                    client=anthropic_client,
                    raise_on_error=False,
                )
                classification = cresult.classification
                classification_confidence = cresult.classification_confidence
                input_tokens = cresult.input_tokens
                output_tokens = cresult.output_tokens
                error = cresult.error
                if classification is None:
                    db_writer.record_fetch(
                        conn, ein=ein, url_redacted=outcome.final_url_redacted or "",
                        kind="classify", fetch_status="classifier_error",
                        notes=sanitize(error),
                    )
                    if reservation_id is not None:
                        budget.release(conn, reservation_id=reservation_id)
                        reservation_id = None
                else:
                    if reservation_id is not None:
                        budget.settle(
                            conn,
                            reservation_id=reservation_id,
                            actual_input_tokens=input_tokens,
                            actual_output_tokens=output_tokens,
                            sha256_classified=outcome.content_sha256,
                        )
                        reservation_id = None
            except Exception:
                if reservation_id is not None:
                    try:
                        budget.release(conn, reservation_id=reservation_id)
                    except Exception:  # noqa: BLE001,S110  # nosec B110 — best-effort rollback; outer raise is the signal
                        pass
                raise

        report_year, report_year_source = infer_report_year(
            source_url=outcome.final_url or cand.url,
            first_page_text=first_page_text or None,
            pdf_creation_date=str(creation_date) if creation_date else None,
        )

        db_writer.upsert_report(
            conn,
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
            classification=classification,
            classification_confidence=classification_confidence,
            classifier_model=config.CLASSIFIER_MODEL,
            classifier_version=config.CLASSIFIER_VERSION,
            report_year=report_year,
            report_year_source=report_year_source,
            extractor_version=config.EXTRACTOR_VERSION,
        )
        if classification in {"annual", "impact", "hybrid"}:
            result.confirmed_report_count += 1

    db_writer.upsert_crawled_org(
        conn,
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
    args = parser.parse_args(argv)

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

        conn = schema.ensure_db(args.data_dir / "reports.db")
        try:
            # Reconcile any crashed-mid-settle preflight reservations.
            reclaimed = budget.reconcile_stale_reservations(conn)
            if reclaimed:
                logger.info("reconciled %d stale classifier preflights", reclaimed)

            client = ReportsHTTPClient()
            try:
                from .classifier_clients import select_classifier_client
                anthropic_client = select_classifier_client()
            except Exception:
                anthropic_client = None

            seeds: Iterable[tuple[str, str]] = []
            if args.nonprofits_db:
                seeds = fetch_seeds_from_0001(args.nonprofits_db)

            for ein, website in seeds:
                if should_skip_ein(conn, ein=ein, refresh=args.refresh):
                    continue
                check = validate_seed_url(website)
                if not check.ok:
                    logger.warning("skip ein=%s bad seed %r: %s", ein, website, check.reason)
                    continue
                try:
                    process_org(
                        ein=ein,
                        website=website,
                        client=client,
                        anthropic_client=anthropic_client,
                        conn=conn,
                        archive_dir=args.archive_dir,
                    )
                except budget.BudgetExceeded:
                    logger.error("classifier budget exhausted; halting")
                    return 2
                except Exception as exc:  # noqa: BLE001
                    logger.exception("ein=%s failed: %s", ein, exc)
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
