"""Async PDF fetch with HEAD+GET + magic-byte verification (Spec 0021, AC20/AC21).

Async equivalent of fetch_pdf.py. PDF structure validation reuses the
existing _validate_pdf_structure (subprocess-per-PDF with kill-on-timeout)
wrapped in ThreadPoolExecutor for async compatibility.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from .fetch_pdf import DownloadOutcome, _validate_pdf_structure, is_pdf_magic

if TYPE_CHECKING:
    from .async_http_client import AsyncHTTPClient

_log = logging.getLogger(__name__)


async def _head_or_skip(
    client: AsyncHTTPClient, url: str
) -> tuple[bool, str]:
    try:
        r = await client.head(url, kind="pdf-head")
    except Exception as exc:  # noqa: BLE001
        return True, f"head_failed:{type(exc).__name__}"
    if r.http_status in (405, 501):
        return True, "head_not_supported"
    if r.status == "network_error":
        return True, "head_network_error"
    if r.http_status == 200:
        ctype = (r.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
        if ctype and not ctype.startswith("application/pdf"):
            return False, f"head_content_type:{ctype}"
    return True, ""


async def download(
    url: str,
    client: AsyncHTTPClient,
    *,
    seed_etld1: str | None = None,
    validate_structure: bool = True,
    thread_pool: ThreadPoolExecutor | None = None,
) -> DownloadOutcome:
    """Fetch url, verify PDF magic, SHA-256, and (optionally) structure."""
    proceed, note = await _head_or_skip(client, url)
    if not proceed:
        return DownloadOutcome(
            status="blocked_content_type",
            url=url,
            final_url=None,
            final_url_redacted=None,
            redirect_chain=[],
            redirect_chain_redacted=[],
            content_sha256=None,
            bytes_read=0,
            content_type=None,
            note=note,
        )

    r = await client.get(url, kind="pdf-get", seed_etld1=seed_etld1)
    if r.status != "ok":
        return DownloadOutcome(
            status=r.status,
            url=url,
            final_url=r.final_url,
            final_url_redacted=r.final_url_redacted,
            redirect_chain=r.redirect_chain,
            redirect_chain_redacted=r.redirect_chain_redacted,
            content_sha256=None,
            bytes_read=r.bytes_read,
            content_type=None,
            note=r.note or r.error,
        )

    body = r.body or b""
    if not is_pdf_magic(body[:32]):
        return DownloadOutcome(
            status="blocked_content_type",
            url=url,
            final_url=r.final_url,
            final_url_redacted=r.final_url_redacted,
            redirect_chain=r.redirect_chain,
            redirect_chain_redacted=r.redirect_chain_redacted,
            content_sha256=None,
            bytes_read=len(body),
            content_type=r.headers.get("Content-Type"),
            note="magic_byte_mismatch",
        )

    if validate_structure:
        loop = asyncio.get_running_loop()
        ok, struct_note = await loop.run_in_executor(
            thread_pool, _validate_pdf_structure, body
        )
        if not ok:
            return DownloadOutcome(
                status="server_error",
                url=url,
                final_url=r.final_url,
                final_url_redacted=r.final_url_redacted,
                redirect_chain=r.redirect_chain,
                redirect_chain_redacted=r.redirect_chain_redacted,
                content_sha256=None,
                bytes_read=len(body),
                content_type=r.headers.get("Content-Type"),
                note=struct_note,
            )

    sha = hashlib.sha256(body).hexdigest()
    return DownloadOutcome(
        status="ok",
        url=url,
        final_url=r.final_url,
        final_url_redacted=r.final_url_redacted,
        redirect_chain=r.redirect_chain,
        redirect_chain_redacted=r.redirect_chain_redacted,
        content_sha256=sha,
        bytes_read=len(body),
        content_type="application/pdf",
        body=body,
    )


__all__ = ["download", "DownloadOutcome"]
