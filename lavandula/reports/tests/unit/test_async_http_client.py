"""Tests for async_http_client.py (AC1-AC9, AC40)."""
from __future__ import annotations

import asyncio
import gzip
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from lavandula.reports.async_http_client import AsyncHTTPClient, _decompress_stream


@pytest.mark.asyncio
async def test_context_manager_creates_session():
    async with AsyncHTTPClient() as client:
        assert client._session is not None
    assert client._session is None


@pytest.mark.asyncio
async def test_get_outside_context_raises():
    client = AsyncHTTPClient()
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.get("https://example.com")


@pytest.mark.asyncio
async def test_head_outside_context_raises():
    client = AsyncHTTPClient()
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.head("https://example.com")


@pytest.mark.asyncio
async def test_blocked_scheme():
    async with AsyncHTTPClient() as client:
        result = await client.get("ftp://example.com/file")
    assert result.status == "blocked_scheme"


@pytest.mark.asyncio
async def test_http_cleartext_blocked():
    async with AsyncHTTPClient() as client:
        result = await client.get("http://example.com")
    assert result.status == "blocked_scheme"
    assert "cleartext" in result.note


@pytest.mark.asyncio
async def test_http_cleartext_allowed():
    async with AsyncHTTPClient(allow_insecure_cleartext=True) as client:
        # Will fail with network error since there's no server, but won't be blocked_scheme
        result = await client.get("http://example.com")
    assert result.status != "blocked_scheme"


@pytest.mark.asyncio
async def test_protocol_relative_url_normalized():
    async with AsyncHTTPClient() as client:
        result = await client.get("//example.com/page")
    # Should try https://example.com/page (will fail with network_error but not blocked)
    assert result.status != "blocked_scheme"


@pytest.mark.asyncio
async def test_unknown_kind_raises():
    async with AsyncHTTPClient() as client:
        with pytest.raises(ValueError, match="unknown fetch kind"):
            await client.get("https://example.com", kind="unknown")


@pytest.mark.asyncio
async def test_empty_host():
    async with AsyncHTTPClient() as client:
        result = await client.get("https:///path")
    assert result.status == "server_error"
    assert result.note == "empty host"


class _MockContent:
    def __init__(self, chunks: list[bytes]):
        self._chunks = iter(chunks)

    async def read(self, n: int) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            return b""


class _MockResponse:
    def __init__(
        self, content_encoding: str = "", chunks: list[bytes] | None = None
    ):
        self.headers = {}
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding
        self.content = _MockContent(chunks or [])


@pytest.mark.asyncio
async def test_decompress_identity():
    resp = _MockResponse(chunks=[b"hello", b" world"])
    body, err = await _decompress_stream(resp, max_bytes=1024)
    assert err is None
    assert body == b"hello world"


@pytest.mark.asyncio
async def test_decompress_gzip():
    raw = gzip.compress(b"decompressed content")
    resp = _MockResponse(content_encoding="gzip", chunks=[raw])
    body, err = await _decompress_stream(resp, max_bytes=1024)
    assert err is None
    assert body == b"decompressed content"


@pytest.mark.asyncio
async def test_decompress_size_cap():
    data = b"A" * 100
    resp = _MockResponse(chunks=[data])
    body, err = await _decompress_stream(resp, max_bytes=50)
    assert err == "size_capped"


@pytest.mark.asyncio
async def test_decompress_blocked_brotli():
    resp = _MockResponse(content_encoding="br", chunks=[b"data"])
    body, err = await _decompress_stream(resp, max_bytes=1024)
    assert err == "blocked_content_type"
    assert body == b""


@pytest.mark.asyncio
async def test_decompress_blocked_deflate():
    resp = _MockResponse(content_encoding="deflate", chunks=[b"data"])
    body, err = await _decompress_stream(resp, max_bytes=1024)
    assert err == "blocked_content_type"


@pytest.mark.asyncio
async def test_decompress_blocked_zstd():
    resp = _MockResponse(content_encoding="zstd", chunks=[b"data"])
    body, err = await _decompress_stream(resp, max_bytes=1024)
    assert err == "blocked_content_type"


@pytest.mark.asyncio
async def test_decompress_gzip_bomb():
    bomb = gzip.compress(b"\x00" * 10_000_000)
    resp = _MockResponse(content_encoding="gzip", chunks=[bomb])
    body, err = await _decompress_stream(resp, max_bytes=1_000)
    assert err == "size_capped"
