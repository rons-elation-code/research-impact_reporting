"""Main crawler entrypoint. Wires Phases 1-4 into the orchestration loop.

Usage:
    python -m lavandula.nonprofits.crawler --limit 50
    python -m lavandula.nonprofits.crawler --refresh
    python -m lavandula.nonprofits.crawler --start-ein 530196605

Exit codes:
    0 — clean completion
    1 — generic error (uncaught exception)
    2 — stop-condition halt (HALT-*.md written)
    3 — another crawler process already holds the lock
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import (
    archive,
    checkpoint,
    config,
    db_writer,
    extract,
    fetcher,
    http_client,
    logging_utils,
    robots,
    schema,
    sitemap,
    stop_conditions,
)
from .logging_utils import sanitize, sanitize_exception
from .url_utils import canonicalize_ein


log = logging.getLogger("lavandula.nonprofits.crawler")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_HALT = 2
EXIT_LOCK_HELD = 3


@dataclass
class CrawlOptions:
    limit: int | None
    refresh: bool
    start_ein: str | None
    no_download: bool
    skip_tls_selftest: bool


def parse_args(argv: list[str] | None = None) -> CrawlOptions:
    p = argparse.ArgumentParser(prog="lavandula-nonprofits-crawler")
    p.add_argument("--limit", type=int, default=None,
                   help="Maximum number of profile fetches in this run.")
    p.add_argument("--refresh", action="store_true",
                   help="Ignore nonprofits table; re-fetch every EIN.")
    p.add_argument("--start-ein", default=None,
                   help="Skip sitemap_entries whose EIN < this value.")
    p.add_argument("--no-download", action="store_true",
                   help="Dry run: enumerate + plan, but do not fetch profiles.")
    p.add_argument("--skip-tls-selftest", action="store_true",
                   help="Internal: used by tests that stand up their own TLS harness.")
    args = p.parse_args(argv)
    return CrawlOptions(
        limit=args.limit,
        refresh=args.refresh,
        start_ein=args.start_ein,
        no_download=args.no_download,
        skip_tls_selftest=args.skip_tls_selftest,
    )


def _acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n{socket.gethostname()}\n".encode())
    return fd


def _write_halt_file(logs_dir: Path, reason: str, *, detail: str = "") -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    path = logs_dir / f"HALT-{sanitize(reason, max_len=40)}-{ts}.md"
    try:
        content = (
            f"# HALT: {sanitize(reason)}\n\n"
            f"Time: {ts} UTC\n"
            f"PID: {os.getpid()}\n"
            f"Host: {sanitize(socket.gethostname())}\n\n"
            f"## Detail\n\n{sanitize(detail, max_len=4000)}\n"
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        # Fallback: emit to stderr and hard-exit. HALT files retained
        # indefinitely are the forensic record; if disk is full we can
        # still signal intent via exit code 2.
        sys.stderr.write(
            f"HALT {reason}: {sanitize_exception(exc)}\n"
        )
    return path


def _enumerate_if_empty(
    client: http_client.ThrottledClient,
    conn,
    policy: robots.RobotsPolicy,
) -> int:
    """Populate sitemap_entries from the live sitemap if empty.

    Returns the number of entries known after enumeration.
    """
    existing = conn.execute("SELECT COUNT(*) FROM sitemap_entries").fetchone()[0]
    if existing:
        return existing

    log.info("Fetching sitemap index %s", config.SITEMAP_INDEX_URL)
    index = client.get(config.SITEMAP_INDEX_URL, content_type_required=False)
    if index.status != "ok" or index.body is None:
        raise RuntimeError(
            f"sitemap index fetch failed: status={index.status} note={index.note}"
        )
    child_urls = sitemap.parse_sitemap_index(index.body)
    log.info("Sitemap index returned %d child sitemaps", len(child_urls))

    inserted = 0
    for child_url in child_urls:
        child = client.get(child_url, content_type_required=False)
        if child.status != "ok" or child.body is None:
            log.warning("Child sitemap fetch failed: %s (%s)", child_url, child.status)
            continue
        label = child_url.rsplit("/", 1)[-1] or child_url
        entries = sitemap.parse_child_sitemap(child.body)
        with conn:
            for loc in entries:
                from .url_utils import ein_from_profile_url as _ein
                ein = _ein(loc.url)
                if not ein:
                    continue
                if ein in config.DISALLOWED_EINS:
                    continue
                if not policy.is_allowed(f"/ein/{ein}"):
                    continue
                db_writer.insert_sitemap_entry(
                    conn, ein=ein, source_sitemap=label, lastmod=loc.lastmod,
                )
                inserted += 1
    log.info("Enumerated %d EINs into sitemap_entries", inserted)
    return inserted


def run(opts: CrawlOptions) -> int:
    logs_dir = config.LOGS
    logging_utils.setup_logging(logs_dir)
    log.info("Starting Lavandula Design nonprofit crawler")

    # Preflight disk check.
    ok, free_gb = stop_conditions.preflight_disk_check(config.RAW)
    if not ok:
        log.error("Preflight disk check failed: %d GB free (need >= %d)",
                  free_gb, config.PREFLIGHT_FREE_GB)
        _write_halt_file(logs_dir, "preflight_disk",
                         detail=f"Only {free_gb} GB free on archive partition.")
        return EXIT_HALT

    # Lock.
    lock_fd = _acquire_lock(config.LOCK_PATH)
    if lock_fd is None:
        log.error("Another crawler process already holds %s", config.LOCK_PATH)
        return EXIT_LOCK_HELD

    # Setup.
    config.DATA.mkdir(parents=True, exist_ok=True)
    config.RAW.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config.DATA, 0o700)
        os.chmod(config.RAW.parent, 0o700)
        os.chmod(config.RAW, 0o700)
    except OSError:
        pass
    archive.sweep_stale_tmpdirs(config.RAW)
    tmpdir = archive.ensure_archive_dir(config.RAW)

    conn = schema.ensure_db(config.DB_PATH)

    # TLS self-test (skippable via flag for tests).
    if not opts.skip_tls_selftest:
        try:
            http_client.tls_self_test()
        except http_client.TLSMisconfigured as exc:
            log.error("TLS self-test failed: %s", sanitize_exception(exc))
            _write_halt_file(logs_dir, "tls_selftest", detail=str(exc))
            return EXIT_HALT

    client = http_client.ThrottledClient()

    # Robots.
    r = client.get(config.ROBOTS_URL, content_type_required=False)
    if r.status != "ok" or r.body is None:
        log.error("robots.txt fetch failed: %s", r.status)
        _write_halt_file(logs_dir, "robots_fetch",
                         detail=f"status={r.status} note={r.note}")
        return EXIT_HALT
    try:
        policy = robots.parse(r.body.decode("utf-8", errors="replace"),
                              ua=config.USER_AGENT)
    except robots.RobotsError as exc:
        log.error("robots.txt parse failed: %s", sanitize_exception(exc))
        _write_halt_file(logs_dir, "robots_parse", detail=str(exc))
        return EXIT_HALT
    if not robots.allows_ein_path(policy):
        log.error("robots.txt now disallows /ein/*")
        _write_halt_file(logs_dir, "robots_disallow_ein")
        return EXIT_HALT

    stop = stop_conditions.StopConditions(archive_root=config.RAW)
    cp = checkpoint.load()
    if not cp.started_at:
        cp.started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # Enumerate sitemap if empty.
    try:
        _enumerate_if_empty(client, conn, policy)
    except Exception as exc:
        log.error("Sitemap enumeration failed: %s", sanitize_exception(exc))
        _write_halt_file(logs_dir, "sitemap_enum", detail=str(exc))
        return EXIT_HALT

    if opts.no_download:
        log.info("--no-download set; enumeration only. Exiting cleanly.")
        return EXIT_OK

    # SIGTERM handler.
    def _on_sigterm(signum, frame):  # noqa: ARG001
        try:
            checkpoint.save(cp)
        except Exception:
            pass
        try:
            _write_halt_file(logs_dir, "sigterm",
                             detail="Received SIGTERM; flushed checkpoint.")
        except Exception:
            sys.stderr.write("SIGTERM: HALT write failed\n")
            os._exit(EXIT_HALT)
        os._exit(EXIT_HALT)

    signal.signal(signal.SIGTERM, _on_sigterm)

    # Iterate EINs.
    fetched = 0
    for ein, source_sitemap, lastmod in db_writer.unfetched_sitemap_entries(
        conn, limit=opts.limit if not opts.refresh else None,
    ):
        if opts.start_ein and ein < opts.start_ein:
            continue
        if opts.limit is not None and fetched >= opts.limit:
            break
        reason = stop.evaluate()
        if reason:
            log.error("Stop condition: %s", reason)
            _write_halt_file(logs_dir, reason, detail=str(stop.state))
            return EXIT_HALT

        outcome = fetcher.fetch_profile(
            client, ein, raw_cn=config.RAW, tmpdir=tmpdir,
        )

        with conn:
            db_writer.insert_fetch_log(
                conn,
                ein=outcome.ein,
                url=outcome.requested_url,
                status_code=outcome.http_status,
                attempt=outcome.attempts,
                is_retry=outcome.attempts > 1,
                fetch_status=outcome.fetch_status,
                elapsed_ms=outcome.elapsed_ms,
                bytes_read=outcome.bytes_read,
                notes=outcome.note,
                error=outcome.error,
            )

            if outcome.fetch_status == "ok" and outcome.body is not None:
                try:
                    profile = extract.extract(outcome.body, ein=outcome.ein)
                    db_writer.upsert_nonprofit(
                        conn, profile,
                        cn_profile_url=outcome.requested_url,
                        content_sha256=outcome.content_sha256 or "",
                        redirected_to_ein=outcome.redirected_to_ein,
                        parse_version=config.PARSE_VERSION,
                    )
                except Exception as exc:
                    log.warning("extract failure for %s: %s", outcome.ein,
                                sanitize_exception(exc))

        stop.observe_fetch(
            outcome.fetch_status,
            retry_after=outcome.retry_after_sec,
            bytes_read=outcome.bytes_read,
        )
        cp.last_ein = outcome.ein
        if outcome.fetch_status == "ok":
            cp.fetched_count += 1
            fetched += 1
        else:
            cp.failed_count += 1
        if fetched % 50 == 0:
            checkpoint.save(cp)

    checkpoint.save(cp)
    log.info("Crawler finished. fetched=%d failed=%d", cp.fetched_count, cp.failed_count)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(argv)
    try:
        return run(opts)
    except KeyboardInterrupt:
        return EXIT_HALT
    except Exception as exc:  # noqa: BLE001
        log.exception("Unhandled exception: %s", sanitize_exception(exc))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
