"""Throttled HTTP client with all security controls from spec 0001.

Design:
  - 3s throttle + jitter (REQUEST_DELAY_SEC ± REQUEST_DELAY_JITTER_SEC)
  - TLS verification always on; startup self-test against a known-bad cert
    endpoint (local ephemeral server; remote expired.badssl.com as secondary)
  - Redirects are NEVER auto-followed by requests; manually validated so only
    https://www.charitynavigator.org targets are accepted
  - Streamed decompression with an explicit decoded-bytes cap; sockets close
    early if the cap is exceeded
  - Cookies reset after every GET (fingerprinting + checkpoint-leak defense)
  - Retry-After handles both HTTP-date and seconds forms
  - Content-Type validated before returning body to caller
"""
from __future__ import annotations

import email.utils
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.cookies import RequestsCookieJar

from . import config
from .logging_utils import sanitize, sanitize_exception


class TLSMisconfigured(RuntimeError):
    """Raised when the TLS self-test suggests verification is disabled."""


class ResponseSizeExceeded(RuntimeError):
    """Decompressed body exceeded MAX_RESPONSE_BYTES."""


class UnexpectedContentType(RuntimeError):
    """Response Content-Type not in ALLOWED_CONTENT_TYPES."""


class CrossHostRedirect(RuntimeError):
    """Server redirected to an unauthorized host/scheme."""


class RateLimited(RuntimeError):
    """Rate limited and retries exhausted."""


class NetworkError(RuntimeError):
    """Transport-level failure after retries."""


@dataclass
class FetchResult:
    """Normalized fetch outcome.

    `status` is the `fetch_log.fetch_status` enum value. `body` holds the
    decoded bytes only on status='ok'. `redirect_chain` is the list of
    absolute URLs visited (including the final one). `retry_after_sec` is
    populated when the final response was a 429 with a parseable header.
    """
    status: str
    http_status: int | None
    body: bytes | None
    final_url: str | None
    redirect_chain: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    bytes_read: int = 0
    elapsed_ms: int = 0
    attempts: int = 0
    retry_after_sec: float | None = None
    note: str = ""
    error: str = ""


# --- Retry-After parsing ------------------------------------------------

def parse_retry_after(header_value: str | None) -> float | None:
    """Parse an RFC 7231 Retry-After header.

    Accepts delta-seconds (integer) or HTTP-date. Returns seconds-until or
    None on failure. Negative / sub-zero values are clamped to 0.
    """
    if not header_value:
        return None
    v = header_value.strip()
    if not v:
        return None
    try:
        if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            return max(0.0, float(v))
        # Some servers send float-seconds.
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    import datetime as _dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    delta = (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    return max(0.0, delta)


# --- TLS self-test ------------------------------------------------------

def _local_bad_cert_server() -> tuple[HTTPServer, threading.Thread]:
    """Start a localhost HTTPS server with an expired self-signed cert.

    Runs in a daemon thread; caller shuts down via `server.shutdown()`.
    """
    # Generate a self-signed expired cert in-memory. Using openssl here would
    # add a dependency; instead we ship a bundled expired cert inside the
    # repo? No — dynamic gen lets every test run fresh. Use cryptography.
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as e:
        raise TLSMisconfigured(
            "cryptography module missing; local TLS self-test unavailable"
        ) from e
    import datetime as _dt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=365)
    past_end = past + _dt.timedelta(days=1)  # expired yesterday
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(past)
        .not_valid_after(past_end)
        .sign(key, hashes.SHA256())
    )
    import tempfile
    cert_dir = Path(tempfile.mkdtemp(prefix="lavandula-tls-"))
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args, **kwargs):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def tls_self_test(*, remote_url: str = "https://expired.badssl.com",
                  budget_sec: float = 30.0) -> None:
    """Verify TLS certificate validation is active.

    Primary: local known-bad-cert server; MUST fail with a cert error.
    Secondary: remote expired.badssl.com; inconclusive (timeout, DNS) is
    warned but not a halt.

    Raises TLSMisconfigured if the local check succeeds (meaning
    verification is disabled somewhere), or if the local server itself
    cannot be stood up.
    """
    try:
        server, _t = _local_bad_cert_server()
    except Exception as exc:
        raise TLSMisconfigured(
            f"Unable to start local bad-cert server: {sanitize_exception(exc)}"
        ) from exc
    try:
        host, port = server.server_address
        local_url = f"https://{host}:{port}/"
        try:
            r = requests.get(local_url, timeout=5, verify=True)
            # If we got a 2xx or any response without SSL failure, verification
            # is disabled — that's the attack.
            raise TLSMisconfigured(
                f"Local expired-cert endpoint returned {r.status_code}; "
                "TLS verification appears disabled"
            )
        except requests.exceptions.SSLError:
            pass  # expected
        except requests.exceptions.ConnectionError as exc:
            # Some platforms raise ConnectionError wrapping SSLError; accept
            # that as pass if the message mentions TLS/SSL.
            msg = str(exc).lower()
            if "ssl" in msg or "certificate" in msg or "tls" in msg:
                return
            raise TLSMisconfigured(
                f"Local TLS check inconclusive: {sanitize_exception(exc)}"
            ) from exc
    finally:
        server.shutdown()

    # Remote cross-check (best-effort, does not gate).
    start = time.monotonic()
    try:
        requests.get(remote_url, timeout=min(budget_sec, 10), verify=True)
        raise TLSMisconfigured(
            f"Remote {remote_url} succeeded despite expired cert; verify config"
        )
    except requests.exceptions.SSLError:
        return
    except requests.exceptions.RequestException:
        # Network is down / DNS broken; do not halt. Local check was
        # authoritative.
        return
    finally:
        _ = time.monotonic() - start


# --- Throttled client --------------------------------------------------

class ThrottledClient:
    """HTTP client that obeys every security and throttle rule in the spec.

    Usage:
        client = ThrottledClient()
        result = client.get(url, ein='530196605')
        # result.status is one of the fetch_log.fetch_status enum values.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        delay_sec: float | None = None,
        jitter_sec: float | None = None,
        max_bytes: int | None = None,
        max_retries: int | None = None,
        allowed_host: str | None = None,
        allowed_scheme: str | None = None,
        allowed_content_types: tuple[str, ...] | None = None,
        sleep: callable = time.sleep,
        monotonic: callable = time.monotonic,
    ) -> None:
        self.user_agent = user_agent or config.USER_AGENT
        self.delay_sec = delay_sec if delay_sec is not None else config.REQUEST_DELAY_SEC
        self.jitter_sec = jitter_sec if jitter_sec is not None else config.REQUEST_DELAY_JITTER_SEC
        self.max_bytes = max_bytes or config.MAX_RESPONSE_BYTES
        self.max_retries = max_retries or config.MAX_RETRIES
        self.allowed_host = allowed_host or config.ALLOWED_REDIRECT_HOST
        self.allowed_scheme = allowed_scheme or config.ALLOWED_REDIRECT_SCHEME
        self.allowed_content_types = allowed_content_types or config.ALLOWED_CONTENT_TYPES
        self._sleep = sleep
        self._monotonic = monotonic

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip, deflate",
        })
        # Cookies are reset after every GET; start with an empty jar.
        self.session.cookies = RequestsCookieJar()
        self.last_request_at: float | None = None
        self.cookie_warnings = 0

    # -- public API -----------------------------------------------------

    def get(
        self,
        url: str,
        *,
        ein: str | None = None,
        allow_cross_host: bool = False,
        content_type_required: bool = True,
    ) -> FetchResult:
        """GET `url` with throttle, retry, redirect validation, size cap.

        `ein` is attached to the fetch_log row.
        `allow_cross_host=True` skips the host allowlist — used for the
        TLS self-test and for sitemap index crawl targets that live on
        the same host but the caller may want a sharper error.
        `content_type_required=False` is used for robots.txt (text/plain).
        """
        parsed = urlparse(url)
        if parsed.scheme != self.allowed_scheme:
            return FetchResult(
                status="server_error",
                http_status=None,
                body=None,
                final_url=url,
                note=sanitize(f"disallowed scheme: {parsed.scheme}"),
            )
        if not allow_cross_host and parsed.hostname != self.allowed_host:
            return FetchResult(
                status="server_error",
                http_status=None,
                body=None,
                final_url=url,
                note=sanitize(f"disallowed host: {parsed.hostname}"),
            )

        attempts_made = 0
        redirect_chain: list[str] = []
        current_url = url
        t_start = self._monotonic()

        for attempt in range(1, self.max_retries + 1):
            attempts_made = attempt
            self._throttle_tick()
            # Reset cookies before every request so we never send state from
            # the prior fetch. (Defense in depth: the server may also reject.)
            self.session.cookies = RequestsCookieJar()
            try:
                resp = self.session.get(
                    current_url,
                    allow_redirects=False,
                    stream=True,
                    timeout=config.REQUEST_TIMEOUT_SEC,
                    verify=True,
                )
            except requests.exceptions.SSLError as exc:
                return FetchResult(
                    status="network_error",
                    http_status=None,
                    body=None,
                    final_url=current_url,
                    redirect_chain=redirect_chain,
                    error=sanitize_exception(exc),
                    attempts=attempts_made,
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )
            except requests.RequestException as exc:
                # Transient network: retry with backoff.
                wait = config.RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                if attempt == self.max_retries:
                    return FetchResult(
                        status="network_error",
                        http_status=None,
                        body=None,
                        final_url=current_url,
                        redirect_chain=redirect_chain,
                        error=sanitize_exception(exc),
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                self._sleep(wait)
                continue

            if resp.headers.get("Set-Cookie"):
                self.cookie_warnings += 1

            # Redirect handling
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    return FetchResult(
                        status="server_error",
                        http_status=resp.status_code,
                        body=None,
                        final_url=current_url,
                        redirect_chain=redirect_chain,
                        note="redirect without Location",
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                # Resolve relative URLs against the current URL.
                from urllib.parse import urljoin
                next_url = urljoin(current_url, location)
                next_parsed = urlparse(next_url)
                if next_parsed.scheme != self.allowed_scheme:
                    return FetchResult(
                        status="server_error",
                        http_status=resp.status_code,
                        body=None,
                        final_url=next_url,
                        redirect_chain=redirect_chain + [next_url],
                        note=sanitize(f"scheme-downgrade redirect to {next_parsed.scheme}"),
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                if not allow_cross_host and next_parsed.hostname != self.allowed_host:
                    return FetchResult(
                        status="server_error",
                        http_status=resp.status_code,
                        body=None,
                        final_url=next_url,
                        redirect_chain=redirect_chain + [next_url],
                        note=sanitize(f"cross-host redirect to {next_parsed.hostname}"),
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                redirect_chain.append(next_url)
                if len(redirect_chain) > config.MAX_REDIRECTS:
                    return FetchResult(
                        status="server_error",
                        http_status=resp.status_code,
                        body=None,
                        final_url=next_url,
                        redirect_chain=redirect_chain,
                        note=f"redirect chain exceeded {config.MAX_REDIRECTS}",
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                current_url = next_url
                # Loop back around to issue the next GET; counts as the
                # same logical attempt, but re-enter throttle.
                continue

            # Status-code handling
            status = resp.status_code
            if status == 429:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                resp.close()
                if attempt == self.max_retries:
                    return FetchResult(
                        status="rate_limited",
                        http_status=status,
                        body=None,
                        final_url=current_url,
                        redirect_chain=redirect_chain,
                        retry_after_sec=retry_after,
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                        note=sanitize(f"Retry-After: {retry_after}"),
                    )
                wait = retry_after if retry_after else (
                    config.RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                )
                # Clamp crazy values so a malicious year-9999 header can't
                # stall the crawl forever. Stop conditions handle >300s
                # at a higher layer.
                wait = min(wait, config.MAX_RETRY_AFTER_SEC * 2)
                self._sleep(wait)
                continue

            if status == 403:
                resp.close()
                return FetchResult(
                    status="forbidden",
                    http_status=status,
                    body=None,
                    final_url=current_url,
                    redirect_chain=redirect_chain,
                    attempts=attempts_made,
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )
            if status == 404:
                resp.close()
                return FetchResult(
                    status="not_found",
                    http_status=status,
                    body=None,
                    final_url=current_url,
                    redirect_chain=redirect_chain,
                    attempts=attempts_made,
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )
            if 500 <= status < 600:
                resp.close()
                if attempt == self.max_retries:
                    return FetchResult(
                        status="server_error",
                        http_status=status,
                        body=None,
                        final_url=current_url,
                        redirect_chain=redirect_chain,
                        attempts=attempts_made,
                        elapsed_ms=int((self._monotonic() - t_start) * 1000),
                    )
                self._sleep(config.RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                continue

            # 2xx path
            if status < 200 or status >= 300:
                resp.close()
                return FetchResult(
                    status="server_error",
                    http_status=status,
                    body=None,
                    final_url=current_url,
                    redirect_chain=redirect_chain,
                    attempts=attempts_made,
                    note=sanitize(f"unexpected status {status}"),
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )

            # Validate Content-Type.
            ctype = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type_required and not any(
                ctype.startswith(t) for t in self.allowed_content_types
            ):
                resp.close()
                return FetchResult(
                    status="server_error",
                    http_status=status,
                    body=None,
                    final_url=current_url,
                    redirect_chain=redirect_chain,
                    attempts=attempts_made,
                    note=sanitize(f"unexpected content-type: {ctype}"),
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )

            # Streamed read with decoded-byte cap.
            total = 0
            chunks: list[bytes] = []
            try:
                for chunk in resp.iter_content(chunk_size=8192, decode_unicode=False):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_bytes:
                        resp.close()
                        return FetchResult(
                            status="size_capped",
                            http_status=status,
                            body=None,
                            final_url=current_url,
                            redirect_chain=redirect_chain,
                            bytes_read=total,
                            attempts=attempts_made,
                            note=f"body exceeded {self.max_bytes} bytes",
                            elapsed_ms=int((self._monotonic() - t_start) * 1000),
                        )
                    chunks.append(chunk)
            except requests.RequestException as exc:
                return FetchResult(
                    status="network_error",
                    http_status=status,
                    body=None,
                    final_url=current_url,
                    redirect_chain=redirect_chain,
                    bytes_read=total,
                    attempts=attempts_made,
                    error=sanitize_exception(exc),
                    elapsed_ms=int((self._monotonic() - t_start) * 1000),
                )
            finally:
                resp.close()

            body = b"".join(chunks)
            # Sanity: Content-Length vs actual decoded bytes (>10× divergence).
            note = ""
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                cl_int = int(cl)
                if cl_int > 0 and (total > cl_int * 10 or cl_int > total * 10):
                    note = sanitize(
                        f"content-length {cl_int} vs actual {total} diverges >10x"
                    )

            self._finish_tick()
            return FetchResult(
                status="ok",
                http_status=status,
                body=body,
                final_url=current_url,
                redirect_chain=redirect_chain,
                headers=dict(resp.headers),
                bytes_read=total,
                attempts=attempts_made,
                elapsed_ms=int((self._monotonic() - t_start) * 1000),
                note=note,
            )

        # Exhausted retries without returning.
        return FetchResult(
            status="network_error",
            http_status=None,
            body=None,
            final_url=current_url,
            redirect_chain=redirect_chain,
            attempts=attempts_made,
            error="retries exhausted without classification",
            elapsed_ms=int((self._monotonic() - t_start) * 1000),
        )

    # -- internal helpers -----------------------------------------------

    def _throttle_tick(self) -> None:
        now = self._monotonic()
        if self.last_request_at is None:
            self.last_request_at = now
            return
        jitter = random.uniform(-self.jitter_sec, self.jitter_sec)
        needed = self.delay_sec + jitter
        wait = self.last_request_at + needed - now
        if wait > 0:
            self._sleep(wait)
        self.last_request_at = self._monotonic()

    def _finish_tick(self) -> None:
        # Reset cookies after every successful fetch.
        self.session.cookies = RequestsCookieJar()
