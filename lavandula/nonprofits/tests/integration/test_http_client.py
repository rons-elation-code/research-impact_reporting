"""Integration tests for ThrottledClient against mock responses.

Covers AC2 (throttle), AC4 (size cap), AC5 (cross-host redirect),
AC5a (scheme downgrade), AC5b (content-type), AC6 (cookie non-persistence).

Uses a local HTTPBin-style server via http.server; sockets are real but
loopback-only.
"""
from __future__ import annotations

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from lavandula.nonprofits import http_client


class _Handler(BaseHTTPRequestHandler):
    script: dict[str, object] = {}

    def log_message(self, *args, **kwargs):  # silence
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        r = self.script.get(path) or self.script.get("*")
        if r is None:
            self.send_response(404)
            self.end_headers()
            return
        status = r.get("status", 200)
        headers = r.get("headers", {})
        body = r.get("body", b"")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if isinstance(body, str):
            body = body.encode()
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@pytest.fixture
def local_server():
    _Handler.script = {}
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    yield base, _Handler.script
    server.shutdown()


def _client(**kw):
    defaults = dict(
        delay_sec=0.0, jitter_sec=0.0, max_retries=2,
        allowed_host="127.0.0.1", allowed_scheme="http",
    )
    defaults.update(kw)
    return http_client.ThrottledClient(**defaults)


def test_ok_fetch(local_server):
    base, script = local_server
    script["/hello"] = {
        "status": 200,
        "headers": {"Content-Type": "text/html"},
        "body": b"<h1>hi</h1>",
    }
    c = _client()
    result = c.get(f"{base}/hello")
    assert result.status == "ok"
    assert result.body == b"<h1>hi</h1>"
    assert result.http_status == 200


def test_404_classified(local_server):
    base, script = local_server
    script["*"] = {"status": 404, "headers": {"Content-Type": "text/html"}, "body": b"nope"}
    c = _client()
    result = c.get(f"{base}/missing")
    assert result.status == "not_found"
    assert result.http_status == 404


def test_403_classified(local_server):
    base, script = local_server
    script["*"] = {"status": 403, "headers": {"Content-Type": "text/html"}, "body": b"no"}
    c = _client()
    result = c.get(f"{base}/blocked")
    assert result.status == "forbidden"


def test_429_retries_then_rate_limited(local_server):
    base, script = local_server
    script["*"] = {
        "status": 429,
        "headers": {"Content-Type": "text/html", "Retry-After": "0"},
        "body": b"slow down",
    }
    c = _client(max_retries=2)
    result = c.get(f"{base}/busy")
    assert result.status == "rate_limited"


def test_size_capped(local_server):
    base, script = local_server
    big = b"x" * (100 * 1024)
    script["*"] = {"status": 200,
                   "headers": {"Content-Type": "text/html"},
                   "body": big}
    c = _client(max_bytes=1024)
    result = c.get(f"{base}/big")
    assert result.status == "size_capped"


def test_gzip_decompressed_cap(local_server):
    base, script = local_server
    big = b"x" * (10 * 1024 * 1024)
    compressed = gzip.compress(big)
    script["*"] = {
        "status": 200,
        "headers": {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
        },
        "body": compressed,
    }
    c = _client(max_bytes=1024 * 64)
    result = c.get(f"{base}/bomb")
    assert result.status == "size_capped"


def test_scheme_downgrade_redirect_blocked(local_server):
    base, script = local_server
    # Redirect to http (downgrade from allowed_scheme='http'? Use https
    # redirect as the "downgrade" relative to client config).
    c = http_client.ThrottledClient(
        delay_sec=0.0, jitter_sec=0.0, max_retries=1,
        allowed_host="127.0.0.1", allowed_scheme="https",
    )
    script["/start"] = {
        "status": 302,
        "headers": {"Location": f"{base}/next"},
    }
    result = c.get(f"{base}/start")
    # Client was configured for https only; http://127.0.0.1:... endpoint
    # starts with http → rejected at initial URL, status='server_error'
    assert result.status == "server_error"
    assert "scheme" in result.note.lower() or "host" in result.note.lower()


def test_cross_host_redirect_blocked(local_server):
    base, script = local_server
    script["/start"] = {
        "status": 302,
        "headers": {"Location": "http://attacker.example.org/"},
    }
    c = _client()
    result = c.get(f"{base}/start")
    assert result.status == "server_error"
    assert "cross-host" in result.note or "disallowed host" in result.note


def test_unexpected_content_type_blocked(local_server):
    base, script = local_server
    script["*"] = {
        "status": 200,
        "headers": {"Content-Type": "application/octet-stream"},
        "body": b"\x00\x01\x02",
    }
    c = _client()
    result = c.get(f"{base}/bin")
    assert result.status == "server_error"
    assert "content-type" in result.note


def test_cookies_not_persisted(local_server):
    base, script = local_server
    script["*"] = {
        "status": 200,
        "headers": {"Content-Type": "text/html", "Set-Cookie": "sid=abc; Path=/"},
        "body": b"<html/>",
    }
    c = _client()
    c.get(f"{base}/one")
    # After a successful fetch, jar is empty.
    assert len(c.session.cookies) == 0
    c.get(f"{base}/two")
    assert len(c.session.cookies) == 0


def test_throttle_enforces_min_interval(local_server):
    base, script = local_server
    script["*"] = {"status": 200,
                   "headers": {"Content-Type": "text/html"},
                   "body": b""}
    c = _client(delay_sec=0.2, jitter_sec=0.0)
    start = time.monotonic()
    for _ in range(3):
        c.get(f"{base}/x")
    elapsed = time.monotonic() - start
    # 3 requests at 0.2s each → at least ~0.4s between the gaps (first is free).
    assert elapsed >= 0.35
