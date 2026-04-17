"""Profile fetcher: fetch + challenge-detect + archive.

Owns archiving end-to-end (per plan Phase 3). The crawler receives a
FetchOutcome from here and does NOT re-archive.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import archive, config
from .http_client import FetchResult, ThrottledClient
from .logging_utils import sanitize
from .url_utils import canonicalize_ein, ein_from_profile_url


@dataclass
class FetchOutcome:
    ein: str
    requested_url: str
    final_url: str | None
    http_status: int | None
    fetch_status: str
    body: bytes | None
    content_sha256: str | None
    archive_path: Path | None
    challenge_path: Path | None
    redirected_to_ein: str | None
    bytes_read: int
    elapsed_ms: int
    attempts: int
    retry_after_sec: float | None
    note: str
    error: str


def _is_challenge(body: bytes) -> bool:
    if not body:
        return False
    head = body[:16384].lower()
    return any(sig.encode("utf-8").lower() in head for sig in config.CHALLENGE_SIGNATURES)


def fetch_profile(
    client: ThrottledClient,
    ein: str,
    *,
    raw_cn: Path,
    tmpdir: Path,
) -> FetchOutcome:
    """Fetch /ein/{ein} and archive the body atomically.

    On cross-EIN redirect (302 to /ein/B), we record B in the outcome but
    the archive file stays keyed by the requested EIN.
    """
    ein = canonicalize_ein(ein)
    url = config.PROFILE_URL_TEMPLATE.format(ein=ein)

    result: FetchResult = client.get(url, ein=ein)

    redirected_to = None
    # If the client followed a cross-EIN redirect internally, detect it.
    if result.redirect_chain:
        last = result.redirect_chain[-1]
        target_ein = ein_from_profile_url(last)
        if target_ein and target_ein != ein:
            try:
                redirected_to = canonicalize_ein(target_ein)
            except ValueError:
                redirected_to = None

    archive_path = None
    challenge_path = None
    content_sha = None

    body = result.body
    if result.status == "ok" and body is not None:
        if _is_challenge(body):
            cpath = archive.challenge_path_for_ein(raw_cn, ein)
            try:
                archive.write_file(cpath, body, tmpdir=tmpdir)
                challenge_path = cpath
            except archive.ArchiveError as exc:
                return FetchOutcome(
                    ein=ein, requested_url=url, final_url=result.final_url,
                    http_status=result.http_status,
                    fetch_status="challenge",
                    body=None, content_sha256=None,
                    archive_path=None, challenge_path=None,
                    redirected_to_ein=redirected_to,
                    bytes_read=result.bytes_read,
                    elapsed_ms=result.elapsed_ms,
                    attempts=result.attempts,
                    retry_after_sec=result.retry_after_sec,
                    note=sanitize(f"challenge body; archive failed: {exc}"),
                    error="",
                )
            return FetchOutcome(
                ein=ein, requested_url=url, final_url=result.final_url,
                http_status=result.http_status,
                fetch_status="challenge",
                body=None, content_sha256=None,
                archive_path=None, challenge_path=challenge_path,
                redirected_to_ein=redirected_to,
                bytes_read=result.bytes_read,
                elapsed_ms=result.elapsed_ms,
                attempts=result.attempts,
                retry_after_sec=result.retry_after_sec,
                note="cloudflare challenge detected",
                error="",
            )

        apath = archive.archive_path_for_ein(raw_cn, ein)
        try:
            archive.write_file(apath, body, tmpdir=tmpdir)
            archive_path = apath
            content_sha = hashlib.sha256(body).hexdigest()
        except archive.SymlinkRefused:
            raise
        except archive.ArchiveError as exc:
            return FetchOutcome(
                ein=ein, requested_url=url, final_url=result.final_url,
                http_status=result.http_status,
                fetch_status="server_error",
                body=body, content_sha256=None,
                archive_path=None, challenge_path=None,
                redirected_to_ein=redirected_to,
                bytes_read=result.bytes_read,
                elapsed_ms=result.elapsed_ms,
                attempts=result.attempts,
                retry_after_sec=result.retry_after_sec,
                note=sanitize(f"archive write failed: {exc}"),
                error="",
            )

    return FetchOutcome(
        ein=ein,
        requested_url=url,
        final_url=result.final_url,
        http_status=result.http_status,
        fetch_status=result.status,
        body=body,
        content_sha256=content_sha,
        archive_path=archive_path,
        challenge_path=challenge_path,
        redirected_to_ein=redirected_to,
        bytes_read=result.bytes_read,
        elapsed_ms=result.elapsed_ms,
        attempts=result.attempts,
        retry_after_sec=result.retry_after_sec,
        note=sanitize(result.note),
        error=sanitize(result.error),
    )
