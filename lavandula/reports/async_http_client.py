"""Async HTTP client for spec 0021 (AC1-AC9, AC40).

Async equivalent of http_client.py using aiohttp. Same controls:
  - Accept-Encoding constrained to gzip, identity (AC4)
  - Manual decompression with decompressed-byte cap (AC3)
  - DNS pinning via AsyncHostPinCache (AC10-AC14)
  - Every-hop redirect gating via check_redirect_chain (AC5)
  - Referer stripped, cookies ignored via DummyCookieJar (AC6)
  - Granular timeouts (AC9)
  - Retry semantics matching the sync client (AC8)
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import zlib
from urllib.parse import urljoin, urlsplit

import aiohttp

from . import config
from .async_host_pin_cache import AsyncHostPinCache
from .async_host_throttle import AsyncHostThrottle, _canonical_host
from .http_client import FetchResult
from .logging_utils import sanitize, sanitize_exception
from .redirect_policy import check_redirect_chain, RedirectCheckResult
from .url_redact import redact_url

_log = logging.getLogger(__name__)

_KIND_TO_CAP = {
    "robots": config.MAX_TEXT_BYTES,
    "sitemap": config.MAX_TEXT_BYTES,
    "homepage": config.MAX_TEXT_BYTES,
    "subpage": config.MAX_TEXT_BYTES,
    "pdf-head": config.MAX_TEXT_BYTES,
    "pdf-get": config.MAX_PDF_BYTES,
    "classify": config.MAX_TEXT_BYTES,
    "resolver-verify": config.MAX_TEXT_BYTES,
    "wayback-cdx":     config.MAX_TEXT_BYTES,
}


def _check_wayback_redirect(redirect_chain: list[str]) -> RedirectCheckResult:
    """If the chain originated at a Wayback host, every hop must remain
    in the archive.org canonical bucket. AC10.1.
    """
    if len(redirect_chain) < 2:
        return RedirectCheckResult(ok=True)
    origin_host = urlsplit(redirect_chain[0]).hostname or ""
    if _canonical_host(origin_host) != "archive.org":
        return RedirectCheckResult(ok=True)
    target_host = urlsplit(redirect_chain[-1]).hostname or ""
    if _canonical_host(target_host) != "archive.org":
        return RedirectCheckResult(
            ok=False,
            reason="blocked_redirect",
            note=sanitize(f"wayback_redirect_to_{target_host}"),
        )
    return RedirectCheckResult(ok=True)


def _http_status_to_fetch_status(code: int) -> str:
    if code == 200:
        return "ok"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not_found"
    if code == 429:
        return "rate_limited"
    if 500 <= code < 600:
        return "server_error"
    return "server_error"


async def _decompress_stream(
    resp: aiohttp.ClientResponse,
    *,
    max_bytes: int,
) -> tuple[bytes, str | None]:
    """Stream body with hard cap on decompressed size.

    Returns (bytes, error). Error can be 'size_capped',
    'blocked_content_type', or None.
    """
    enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
    if enc not in ("", "identity", "gzip"):
        return b"", "blocked_content_type"

    decomp = zlib.decompressobj(zlib.MAX_WBITS | 16) if enc == "gzip" else None

    total = 0
    out = io.BytesIO()
    try:
        while True:
            chunk = await resp.content.read(8192)
            if not chunk:
                break
            if decomp is not None:
                try:
                    decoded = decomp.decompress(chunk, max_bytes + 1 - total)
                except zlib.error:
                    return b"", "blocked_content_type"
            else:
                decoded = chunk
            if decoded:
                total += len(decoded)
                if total > max_bytes:
                    return b"", "size_capped"
                out.write(decoded)
            if decomp is not None and decomp.unconsumed_tail:
                while decomp.unconsumed_tail:
                    try:
                        more = decomp.decompress(
                            decomp.unconsumed_tail, max_bytes + 1 - total
                        )
                    except zlib.error:
                        return b"", "blocked_content_type"
                    if more:
                        total += len(more)
                        if total > max_bytes:
                            return b"", "size_capped"
                        out.write(more)
                    else:
                        break
        if decomp is not None:
            tail = decomp.flush()
            if tail:
                total += len(tail)
                if total > max_bytes:
                    return b"", "size_capped"
                out.write(tail)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return b"", "network_error"
    return out.getvalue(), None


class AsyncHTTPClient:
    """Async context manager wrapping aiohttp.ClientSession."""

    def __init__(
        self,
        *,
        throttle: AsyncHostThrottle | None = None,
        pin_cache: AsyncHostPinCache | None = None,
        allow_insecure_cleartext: bool = False,
    ) -> None:
        self._throttle = throttle or AsyncHostThrottle()
        self._pin_cache = pin_cache or AsyncHostPinCache()
        self._allow_insecure_cleartext = allow_insecure_cleartext
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> AsyncHTTPClient:
        connector = aiohttp.TCPConnector(
            limit=500,
            limit_per_host=2,
            use_dns_cache=False,
            resolver=self._pin_cache,
            keepalive_timeout=60,
        )
        timeout = aiohttp.ClientTimeout(
            total=30, connect=10, sock_connect=10, sock_read=15
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept-Encoding": config.ACCEPT_ENCODING,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.1",
            },
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _check_open(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "AsyncHTTPClient must be used as an async context manager"
            )
        return self._session

    async def _maybe_honor_wayback_retry_after(
        self, host: str, resp: aiohttp.ClientResponse,
    ) -> None:
        """AC17.2: honor Retry-After from Wayback hosts only."""
        if _canonical_host(host) != "archive.org":
            return
        if resp.status not in (429, 503):
            return
        retry_after = resp.headers.get("Retry-After")
        if not retry_after:
            return
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            return
        delay = min(max(delay, 0.0), 60.0)
        if delay > 0:
            _log.info("Wayback Retry-After=%s, sleeping", retry_after)
            await asyncio.sleep(delay)

    async def get(
        self,
        url: str,
        *,
        kind: str = "homepage",
        seed_etld1: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_override: float | None = None,
    ) -> FetchResult:
        session = self._check_open()
        if kind not in _KIND_TO_CAP:
            raise ValueError(f"unknown fetch kind: {kind!r}")
        cap = _KIND_TO_CAP[kind]

        if url.startswith("//"):
            url = "https:" + url

        parsed = urlsplit(url)
        host = parsed.hostname or ""
        scheme = parsed.scheme.lower()

        loop = asyncio.get_running_loop()
        t_start = loop.time()
        redirect_chain: list[str] = [url]

        if scheme not in ("http", "https"):
            return FetchResult(
                status="blocked_scheme",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                elapsed_ms=int((loop.time() - t_start) * 1000),
            )
        if scheme == "http" and not self._allow_insecure_cleartext:
            return FetchResult(
                status="blocked_scheme",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                note="http cleartext not allowed",
                elapsed_ms=int((loop.time() - t_start) * 1000),
            )
        if not host:
            return FetchResult(
                status="server_error",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                note="empty host",
                elapsed_ms=int((loop.time() - t_start) * 1000),
            )

        current_url = url
        for _hop in range(config.MAX_REDIRECTS + 2):
            parsed = urlsplit(current_url)
            host = parsed.hostname or ""

            async with self._throttle.request(host):
                headers: dict[str, str] = {"Referer": ""}
                if extra_headers:
                    headers.update(extra_headers)
                request_kwargs: dict = {
                    "allow_redirects": False,
                    "headers": headers,
                }
                if timeout_override is not None:
                    request_kwargs["timeout"] = aiohttp.ClientTimeout(
                        total=timeout_override,
                    )
                try:
                    resp = await session.get(
                        current_url,
                        **request_kwargs,
                    )
                except aiohttp.ClientSSLError as exc:
                    return FetchResult(
                        status="network_error",
                        final_url=current_url,
                        final_url_redacted=redact_url(current_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        error=sanitize_exception(exc),
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    OSError,
                ) as exc:
                    return FetchResult(
                        status="network_error",
                        final_url=current_url,
                        final_url_redacted=redact_url(current_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        error=sanitize_exception(exc),
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )

            status_code = resp.status
            if 300 <= status_code < 400 and status_code != 304:
                location = resp.headers.get("Location")
                resp.release()
                if not location:
                    return FetchResult(
                        status="server_error",
                        http_status=status_code,
                        final_url=current_url,
                        final_url_redacted=redact_url(current_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        note="redirect without Location",
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )
                next_url = urljoin(current_url, location)
                redirect_chain.append(next_url)

                # AC10.1: Wayback cross-host redirect rejection
                wb_check = _check_wayback_redirect(redirect_chain)
                if not wb_check.ok:
                    return FetchResult(
                        status=wb_check.reason,
                        http_status=status_code,
                        final_url=next_url,
                        final_url_redacted=redact_url(next_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        note=wb_check.note,
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )

                if seed_etld1 is not None:
                    check = check_redirect_chain(
                        redirect_chain, seed_etld1=seed_etld1
                    )
                    if not check.ok:
                        return FetchResult(
                            status=check.reason,
                            http_status=status_code,
                            final_url=next_url,
                            final_url_redacted=redact_url(next_url),
                            redirect_chain=redirect_chain,
                            redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                            kind=kind,
                            note=check.note,
                            elapsed_ms=int((loop.time() - t_start) * 1000),
                        )
                elif len(redirect_chain) - 1 > config.MAX_REDIRECTS:
                    return FetchResult(
                        status="server_error",
                        http_status=status_code,
                        final_url=next_url,
                        final_url_redacted=redact_url(next_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        note="redirect_chain_too_long",
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )
                current_url = next_url
                continue

            # AC17.2: honor Retry-After from Wayback on 429/503
            await self._maybe_honor_wayback_retry_after(host, resp)

            body: bytes | None = b""
            if status_code == 200:
                body_bytes, err = await _decompress_stream(resp, max_bytes=cap)
                resp.release()
                if err == "size_capped":
                    return FetchResult(
                        status="size_capped",
                        http_status=status_code,
                        final_url=current_url,
                        final_url_redacted=redact_url(current_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        bytes_read=cap + 1,
                        note=f"decompressed > {cap} bytes",
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )
                if err == "blocked_content_type":
                    return FetchResult(
                        status="blocked_content_type",
                        http_status=status_code,
                        final_url=current_url,
                        final_url_redacted=redact_url(current_url),
                        redirect_chain=redirect_chain,
                        redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                        kind=kind,
                        note="unsupported content-encoding",
                        elapsed_ms=int((loop.time() - t_start) * 1000),
                    )
                body = body_bytes
            else:
                resp.release()
                body = None

            return FetchResult(
                status=_http_status_to_fetch_status(status_code),
                http_status=status_code,
                body=body,
                final_url=current_url,
                final_url_redacted=redact_url(current_url),
                redirect_chain=redirect_chain,
                redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                headers=dict(resp.headers),
                bytes_read=len(body) if body is not None else 0,
                kind=kind,
                elapsed_ms=int((loop.time() - t_start) * 1000),
            )

        return FetchResult(
            status="server_error",
            final_url=current_url,
            final_url_redacted=redact_url(current_url),
            redirect_chain=redirect_chain,
            redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
            kind=kind,
            note="redirect_chain_too_long",
            elapsed_ms=int((loop.time() - t_start) * 1000),
        )

    async def head(self, url: str, *, kind: str = "pdf-head") -> FetchResult:
        session = self._check_open()
        if url.startswith("//"):
            url = "https:" + url

        loop = asyncio.get_running_loop()
        t_start = loop.time()
        parsed = urlsplit(url)
        host = parsed.hostname or ""

        async with self._throttle.request(host):
            try:
                resp = await session.head(
                    url,
                    allow_redirects=False,
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                return FetchResult(
                    status="network_error",
                    final_url=url,
                    final_url_redacted=redact_url(url),
                    kind=kind,
                    error=sanitize_exception(exc),
                    elapsed_ms=int((loop.time() - t_start) * 1000),
                )

        return FetchResult(
            status=_http_status_to_fetch_status(resp.status),
            http_status=resp.status,
            body=None,
            final_url=url,
            final_url_redacted=redact_url(url),
            headers=dict(resp.headers),
            kind=kind,
            elapsed_ms=int((loop.time() - t_start) * 1000),
        )


__all__ = ["AsyncHTTPClient"]
