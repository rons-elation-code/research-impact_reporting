"""Tests for async_host_throttle.py (AC7)."""
from __future__ import annotations

import asyncio

import pytest

from lavandula.reports.async_host_throttle import AsyncHostThrottle


@pytest.mark.asyncio
async def test_first_request_no_delay():
    throttle = AsyncHostThrottle(min_interval_sec=3.0, jitter_sec=0.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with throttle.request("example.com"):
        pass
    elapsed = loop.time() - t0
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_second_request_delayed():
    throttle = AsyncHostThrottle(min_interval_sec=0.2, jitter_sec=0.0)
    async with throttle.request("example.com"):
        pass
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with throttle.request("example.com"):
        pass
    elapsed = loop.time() - t0
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_different_hosts_no_delay():
    throttle = AsyncHostThrottle(min_interval_sec=10.0, jitter_sec=0.0)
    async with throttle.request("a.com"):
        pass
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with throttle.request("b.com"):
        pass
    elapsed = loop.time() - t0
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_same_host_serialized():
    throttle = AsyncHostThrottle(min_interval_sec=0.1, jitter_sec=0.0)
    results = []

    async def worker(name: str):
        async with throttle.request("host.com"):
            results.append(name)

    await asyncio.gather(worker("a"), worker("b"))
    assert len(results) == 2


@pytest.mark.asyncio
async def test_release_on_exception():
    throttle = AsyncHostThrottle(min_interval_sec=0.1, jitter_sec=0.0)
    with pytest.raises(ValueError):
        async with throttle.request("host.com"):
            raise ValueError("boom")
    async with throttle.request("host.com"):
        pass


@pytest.mark.asyncio
async def test_reset():
    throttle = AsyncHostThrottle(min_interval_sec=10.0, jitter_sec=0.0)
    async with throttle.request("host.com"):
        pass
    throttle.reset()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with throttle.request("host.com"):
        pass
    elapsed = loop.time() - t0
    assert elapsed < 0.1
