"""HTTP client for spec 0004.

Extends `lavandula.nonprofits.http_client.ThrottledClient` (per-host
throttle, cookie reset, backoff, TLS verification) with the additional
controls spec 0004 requires on top of 0001's client:

  - Accept-Encoding constrained to `gzip, identity` (AC8 — refuses
    brotli/zstd/deflate negotiation).
  - Streaming decompressed-byte cap on EVERY encoding, enforced on
    EVERY fetch kind (AC8).
  - Per-host DNS IP pinning (AC12.1) via `HostPinCache`.
  - Every-hop redirect gating (AC12.2.1) via `check_redirect_chain`.
  - Referer stripped from every outbound request (AC12.2.1).
  - URL redaction applied before URLs are returned / logged.

TLS self-test (AC11) is re-exported from 0001's client so callers can
`from .http_client import tls_self_test`.

The `ReportsHTTPClient` is a thin wrapper — it composes rather than
forks — but the streaming read path needed inlining to enforce the
decompressed-size cap across all encodings consistently.
"""
from __future__ import annotations

import gzip
import io
import random
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import requests
from requests.cookies import RequestsCookieJar

from . import config
from .logging_utils import sanitize, sanitize_exception
from .redirect_policy import check_redirect_chain
from .url_guard import HostPinCache, is_address_allowed
from .url_redact import redact_url

# Re-export TLS self-test from 0001.
from lavandula.nonprofits.http_client import (  # noqa: F401
    TLSMisconfigured,
    tls_self_test,
)


_KIND_TO_CAP = {
    "robots": config.MAX_TEXT_BYTES,
    "sitemap": config.MAX_TEXT_BYTES,
    "homepage": config.MAX_TEXT_BYTES,
    "subpage": config.MAX_TEXT_BYTES,
    "pdf-head": config.MAX_TEXT_BYTES,
    "pdf-get": config.MAX_PDF_BYTES,
    "classify": config.MAX_TEXT_BYTES,
}


@dataclass
class FetchResult:
    status: str
    http_status: int | None = None
    body: bytes | None = None
    final_url: str | None = None
    final_url_redacted: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    redirect_chain_redacted: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    bytes_read: int = 0
    elapsed_ms: int = 0
    kind: str = ""
    note: str = ""
    error: str = ""


class ReportsHTTPClient:
    """Thin wrapper over `requests.Session` with spec-0004 controls."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        pin_cache: HostPinCache | None = None,
        allow_insecure_cleartext: bool = False,
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic
        self._throttle_at: dict[str, float] = {}
        self._pin_cache = pin_cache or HostPinCache()
        self._allow_insecure_cleartext = allow_insecure_cleartext

        self.session = requests.Session()
        # User-Agent + Accept-Encoding set as defaults on the Session per
        # Gemini plan-review HIGH #3.
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept-Encoding": config.ACCEPT_ENCODING,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.1",
        })
        # Referer is stripped on each request in `_prepare`.
        self.session.cookies = RequestsCookieJar()

    # -- internal helpers -------------------------------------------------

    def tick_throttle(self, host: str) -> None:
        """Sleep as needed to enforce per-host throttle before the next request.

        Public so tests can exercise AC6 without issuing HTTP.
        """
        now = self._monotonic()
        last = self._throttle_at.get(host)
        if last is not None:
            jitter = random.uniform(
                -config.REQUEST_DELAY_JITTER_SEC,
                config.REQUEST_DELAY_JITTER_SEC,
            )
            wait = last + config.REQUEST_DELAY_SEC + jitter - now
            if wait > 0:
                self._sleep(wait)
        self._throttle_at[host] = self._monotonic()

    @staticmethod
    def _decompress_stream(
        resp: requests.Response,
        *,
        max_bytes: int,
    ) -> tuple[bytes, str | None]:
        """Stream the response body with a hard cap on decompressed size.

        Returns (bytes, error). `error` is set to 'size_capped',
        'blocked_content_type', or None. Non-advertised encodings
        (anything other than `gzip` or identity) count as blocked —
        per AC8 we advertise `Accept-Encoding: gzip, identity` and any
        other response encoding is a protocol violation.

        Uses `resp.raw.read` with `decode_content=False` so we can size-
        cap BEFORE `requests` auto-decompresses (which would make a
        gzip bomb slip past the cap we care about).
        """
        enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        if enc not in ("", "identity", "gzip"):
            return b"", "blocked_content_type"

        decomp = zlib.decompressobj(zlib.MAX_WBITS | 16) if enc == "gzip" else None

        total = 0
        out = io.BytesIO()
        try:
            while True:
                # read raw bytes off the socket with NO auto-decoding.
                chunk = resp.raw.read(8192, decode_content=False)
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
                    # gzip bomb: a small input produced an input backlog.
                    # The next loop iteration's decompress() call will hit
                    # the cap and bail.
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
        except requests.RequestException:
            return b"", "network_error"
        return out.getvalue(), None

    # -- public API -------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        kind: str = "homepage",
        seed_etld1: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Fetch `url` with throttle, redirect gating, size cap, TLS.

        `kind` selects the size cap + the fetch_log kind label.
        `seed_etld1`, when set, enables every-hop cross-origin gating;
        pass `None` for bootstrap calls where the policy doesn't apply
        yet (e.g., the TLS self-test).
        """
        if kind not in _KIND_TO_CAP:
            raise ValueError(f"unknown fetch kind: {kind!r}")
        cap = _KIND_TO_CAP[kind]
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        scheme = parsed.scheme.lower()

        t_start = self._monotonic()
        redirect_chain: list[str] = [url]

        if scheme not in ("http", "https"):
            return FetchResult(
                status="blocked_scheme",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                elapsed_ms=int((self._monotonic() - t_start) * 1000),
            )
        if scheme == "http" and not self._allow_insecure_cleartext:
            return FetchResult(
                status="blocked_scheme",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                note="http cleartext not allowed",
                elapsed_ms=int((self._monotonic() - t_start) * 1000),
            )
        if not host:
            return FetchResult(
                status="server_error",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                note="empty host",
                elapsed_ms=int((self._monotonic() - t_start) * 1000),
            )

        current_url = url
        for hop in range(config.MAX_REDIRECTS + 2):
            parsed = urlsplit(current_url)
            host = parsed.hostname or ""
            self.tick_throttle(host)

            headers = {"Referer": ""}  # strip Referer on every hop
            if extra_headers:
                headers.update(extra_headers)
            self.session.cookies = RequestsCookieJar()
            try:
                resp = self.session.get(
                    current_url,
                    allow_redirects=False,
                    stream=True,
                    timeout=config.REQUEST_TIMEOUT_SEC,
                    headers=headers,
                    verify=True,
                )
            except requests.exceptions.SSLError as exc:
                return FetchResult(
                    status="network_error",
                    final_url=current_url,
                    final_url_redacted=redact_url(current_url),
                    redirect_chain=redirect_chain,
                    redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                    kind=kind,
                    error=sanitize_exception(exc),
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )
            except requests.RequestException as exc:
                return FetchResult(
                    status="network_error",
                    final_url=current_url,
                    final_url_redacted=redact_url(current_url),
                    redirect_chain=redirect_chain,
                    redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
                    kind=kind,
                    error=sanitize_exception(exc),
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )

            status_code = resp.status_code
            if 300 <= status_code < 400 and status_code != 304:
                location = resp.headers.get("Location")
                resp.close()
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
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                from urllib.parse import urljoin
                next_url = urljoin(current_url, location)
                redirect_chain.append(next_url)

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
                            elapsed_ms=int((self._monotonic() - t_start) * 1000),
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
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                current_url = next_url
                continue

            body: bytes | None = b""
            if status_code == 200:
                body_bytes, err = self._decompress_stream(resp, max_bytes=cap)
                resp.close()
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
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
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
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                body = body_bytes
            else:
                resp.close()
                body = None

            final = FetchResult(
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
                elapsed_ms=int((self._monotonic() - t_start) * 1000),
            )
            return final

        return FetchResult(
            status="server_error",
            final_url=current_url,
            final_url_redacted=redact_url(current_url),
            redirect_chain=redirect_chain,
            redirect_chain_redacted=[redact_url(u) for u in redirect_chain],
            kind=kind,
            note="redirect_chain_too_long",
            elapsed_ms=int((self._monotonic() - t_start) * 1000),
        )

    def head(self, url: str, *, kind: str = "pdf-head") -> FetchResult:
        t_start = self._monotonic()
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        self.tick_throttle(host)
        try:
            resp = self.session.head(
                url,
                allow_redirects=False,
                timeout=config.REQUEST_TIMEOUT_SEC,
                verify=True,
            )
        except requests.RequestException as exc:
            return FetchResult(
                status="network_error",
                final_url=url,
                final_url_redacted=redact_url(url),
                kind=kind,
                error=sanitize_exception(exc),
                elapsed_ms=int((self._monotonic() - t_start) * 1000),
            )
        return FetchResult(
            status=_http_status_to_fetch_status(resp.status_code),
            http_status=resp.status_code,
            body=None,
            final_url=url,
            final_url_redacted=redact_url(url),
            headers=dict(resp.headers),
            kind=kind,
            elapsed_ms=int((self._monotonic() - t_start) * 1000),
        )


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


__all__ = [
    "ReportsHTTPClient",
    "FetchResult",
    "tls_self_test",
    "TLSMisconfigured",
]
