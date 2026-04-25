"""Tests for async_host_pin_cache.py (AC10-AC14)."""
from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from lavandula.reports.async_host_pin_cache import AsyncHostPinCache


def _make_gai_result(ip: str, family: int = socket.AF_INET, port: int = 0):
    return [(family, socket.SOCK_STREAM, 0, "", (ip, port))]


@pytest.mark.asyncio
async def test_positive_pin_cached():
    cache = AsyncHostPinCache()
    gai_results = _make_gai_result("93.184.216.34")

    with patch("socket.getaddrinfo", return_value=gai_results) as mock_gai:
        result1 = await cache.resolve("example.com", 443)
        result2 = await cache.resolve("example.com", 443)

    assert mock_gai.call_count == 1
    assert result1[0]["host"] == "93.184.216.34"
    assert result1[0]["hostname"] == "example.com"
    assert result2[0]["host"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_negative_pin_cached():
    cache = AsyncHostPinCache()
    gai_results = _make_gai_result("127.0.0.1")

    with patch("socket.getaddrinfo", return_value=gai_results) as mock_gai:
        with pytest.raises(aiohttp.ClientConnectorError):
            await cache.resolve("evil.com", 443)
        with pytest.raises(aiohttp.ClientConnectorError):
            await cache.resolve("evil.com", 443)

    assert mock_gai.call_count == 1


@pytest.mark.asyncio
async def test_ipv4_preferred():
    cache = AsyncHostPinCache()
    gai_results = [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2607:f8b0:4004:800::200e", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443)),
    ]

    with patch("socket.getaddrinfo", return_value=gai_results):
        result = await cache.resolve("example.com", 443)

    assert result[0]["host"] == "93.184.216.34"
    assert result[0]["family"] == socket.AF_INET


@pytest.mark.asyncio
async def test_ipv6_fallback():
    cache = AsyncHostPinCache()
    gai_results = [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2607:f8b0:4004:800::200e", 443, 0, 0)),
    ]

    with patch("socket.getaddrinfo", return_value=gai_results):
        result = await cache.resolve("example.com", 443)

    assert result[0]["host"] == "2607:f8b0:4004:800::200e"
    assert result[0]["family"] == socket.AF_INET6


@pytest.mark.asyncio
async def test_hostname_preserved_for_sni():
    cache = AsyncHostPinCache()
    gai_results = _make_gai_result("93.184.216.34")

    with patch("socket.getaddrinfo", return_value=gai_results):
        result = await cache.resolve("secure.example.com", 443)

    assert result[0]["hostname"] == "secure.example.com"
    assert result[0]["host"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_private_ip_rejected():
    cache = AsyncHostPinCache()
    for private_ip in ["10.0.0.1", "192.168.1.1", "172.16.0.1"]:
        gai_results = _make_gai_result(private_ip)
        with patch("socket.getaddrinfo", return_value=gai_results):
            with pytest.raises(aiohttp.ClientConnectorError):
                await cache.resolve(f"host-{private_ip}.com", 443)


@pytest.mark.asyncio
async def test_cloud_metadata_rejected():
    cache = AsyncHostPinCache()
    gai_results = _make_gai_result("169.254.169.254")
    with patch("socket.getaddrinfo", return_value=gai_results):
        with pytest.raises(aiohttp.ClientConnectorError):
            await cache.resolve("metadata.internal", 443)


@pytest.mark.asyncio
async def test_each_host_resolved_independently():
    cache = AsyncHostPinCache()
    gai_a = _make_gai_result("1.2.3.4")
    gai_b = _make_gai_result("5.6.7.8")

    with patch("socket.getaddrinfo", side_effect=[gai_a, gai_b]) as mock_gai:
        result_a = await cache.resolve("a.com", 443)
        result_b = await cache.resolve("b.com", 443)

    assert result_a[0]["host"] == "1.2.3.4"
    assert result_b[0]["host"] == "5.6.7.8"
    assert mock_gai.call_count == 2


@pytest.mark.asyncio
async def test_close_is_noop():
    cache = AsyncHostPinCache()
    await cache.close()
