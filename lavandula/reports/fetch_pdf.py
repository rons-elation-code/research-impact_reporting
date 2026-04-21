"""PDF fetch with HEAD+GET + magic-byte verification (AC7).

`download(url, client)` issues a HEAD probe first (cheap early bail on
non-PDF Content-Type); on 405/501 we fall through to GET anyway, per
Codex red-team-plan — some hosts don't support HEAD.

The GET path verifies the first 8 bytes start with `%PDF-1.` (tolerating
a UTF-8 BOM) BEFORE handing bytes to the sandbox. A cheap
structural-validity check via pypdf (Gemini plan-review HIGH #1) runs
in an isolated subprocess BEFORE the heavier sandbox, with a 2-second
wall-time cap enforced by the parent process.
"""
from __future__ import annotations

import hashlib
import io
import logging
import multiprocessing
import queue
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .http_client import FetchResult, ReportsHTTPClient


_log = logging.getLogger(__name__)

_BOM = b"\xef\xbb\xbf"
_PDF_HEADER = b"%PDF-1."
_STRUCTURE_TIMEOUT_SEC = 2.0


def is_pdf_magic(data: bytes) -> bool:
    """True iff `data` starts with the PDF magic (tolerating BOM)."""
    if not data:
        return False
    b = data[len(_BOM):] if data.startswith(_BOM) else data
    return b.startswith(_PDF_HEADER)


@dataclass
class DownloadOutcome:
    status: str               # 'ok' or a fetch_log.fetch_status value
    url: str
    final_url: str | None
    final_url_redacted: str | None
    redirect_chain: list[str]
    redirect_chain_redacted: list[str]
    content_sha256: str | None
    bytes_read: int
    content_type: str | None
    note: str = ""
    body: bytes | None = None


def _validate_pdf_structure_inner(pdf_bytes: bytes) -> tuple[bool, str]:
    """Actual parser call, intended to run in an isolated subprocess."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return True, "pypdf_missing"
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        _ = len(reader.pages)
    except PdfReadError as exc:
        return False, f"pdf_malformed:{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        return False, f"pdf_malformed:{type(exc).__name__}"
    return True, ""


def _validate_pdf_structure_worker(
    pdf_bytes: bytes,
    result_q: "multiprocessing.queues.Queue[tuple[bool, str]]",
) -> None:
    """Run the structure check in a subprocess and ship back the result."""
    try:
        result_q.put(_validate_pdf_structure_inner(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        result_q.put((False, f"pdf_malformed:{type(exc).__name__}"))


def _validate_pdf_structure(pdf_bytes: bytes) -> tuple[bool, str]:
    """Cheap structural-validity check (Gemini plan-review HIGH #1).

    Runs in an isolated subprocess with a hard parent-enforced timeout
    so malformed PDFs cannot leak timer state into the main crawler.
    Returns (ok, note). On any parser exception, worker crash, or
    timeout the PDF is considered malformed and NOT written to archive.
    """
    ctx = multiprocessing.get_context("spawn")
    result_q = ctx.Queue(maxsize=1)
    proc = ctx.Process(
        target=_validate_pdf_structure_worker,
        args=(pdf_bytes, result_q),
        daemon=True,
    )
    proc.start()
    proc.join(_STRUCTURE_TIMEOUT_SEC)

    if proc.is_alive():
        proc.terminate()
        proc.join(1.0)
        if proc.is_alive():
            proc.kill()
            proc.join(1.0)
        return False, "pdf_structure_timeout"

    try:
        return result_q.get_nowait()
    except queue.Empty:
        return False, "pdf_structure_worker_failed"


def _head_or_skip(client, url: str) -> tuple[bool, str]:
    """Run HEAD; return (proceed_to_get, note).

    - 200 with Content-Type pdf → proceed.
    - 200 with non-PDF Content-Type → skip (blocked_content_type).
    - 405 / 501 / network_error → proceed (GET fallback).
    """
    try:
        r = client.head(url, kind="pdf-head")
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


def download(
    url: str,
    client: "ReportsHTTPClient",
    *,
    seed_etld1: str | None = None,
    validate_structure: bool = True,
) -> DownloadOutcome:
    """Fetch `url`, verify PDF magic, SHA-256, and (optionally) structure.

    On any gate failure returns an Outcome with `status != 'ok'` and no
    archived bytes. The caller drives the archive write.
    """
    proceed, note = _head_or_skip(client, url)
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

    r = client.get(url, kind="pdf-get", seed_etld1=seed_etld1)
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
        ok, struct_note = _validate_pdf_structure(body)
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


__all__ = ["is_pdf_magic", "download", "DownloadOutcome"]
