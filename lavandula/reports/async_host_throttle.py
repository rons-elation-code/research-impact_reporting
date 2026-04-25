"""Async per-host throttle using asyncio primitives (Spec 0021, AC7).

Async equivalent of host_throttle.py. Uses asyncio.Semaphore(1) per host
to serialize requests, and asyncio.sleep for politeness gaps. NOT
thread-safe — must be called from a single event loop.
"""
from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator

from . import config


def _canonical_host(host: str) -> str:
    """Normalize host for throttle bucketing.

    AC17.3: web.archive.org and archive.org share one bucket
    (same Fastly CDN; treating them separately would exceed
    Wayback's per-IP rate limit).
    """
    h = host.lower().strip()
    if h == "archive.org" or h == "web.archive.org" or h.endswith(".archive.org"):
        return "archive.org"
    return h


class AsyncHostThrottle:
    """Per-host rate limiter using asyncio primitives."""

    def __init__(
        self,
        *,
        min_interval_sec: float | None = None,
        jitter_sec: float | None = None,
        host_overrides: dict[str, float] | None = None,
    ) -> None:
        self._min_interval = (
            config.REQUEST_DELAY_SEC if min_interval_sec is None else min_interval_sec
        )
        self._jitter = (
            config.REQUEST_DELAY_JITTER_SEC if jitter_sec is None else jitter_sec
        )
        self._host_overrides = host_overrides or {}
        self._init_lock = asyncio.Lock()
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_fetch: dict[str, float] = {}

    def _interval_for(self, canonical: str) -> float:
        return self._host_overrides.get(canonical, self._min_interval)

    async def _get_semaphore(self, canonical: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(canonical)
        if sem is not None:
            return sem
        async with self._init_lock:
            sem = self._semaphores.get(canonical)
            if sem is None:
                sem = asyncio.Semaphore(1)
                self._semaphores[canonical] = sem
            return sem

    @asynccontextmanager
    async def request(self, host: str) -> AsyncIterator[None]:
        """Acquire host slot, sleep for politeness gap, yield, release."""
        canonical = _canonical_host(host)
        sem = await self._get_semaphore(canonical)
        await sem.acquire()
        try:
            interval = self._interval_for(canonical)
            loop = asyncio.get_running_loop()
            now = loop.time()
            last = self._last_fetch.get(canonical)
            if last is not None:
                # S311/B311 OK: jitter is a politeness tweak, not a security primitive.
                jitter = random.uniform(-self._jitter, self._jitter)  # noqa: S311  # nosec B311
                next_allowed = last + interval + jitter
                delay = max(0.0, next_allowed - now)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_fetch[canonical] = asyncio.get_running_loop().time()
            yield
        finally:
            sem.release()

    def reset(self) -> None:
        """Clear all state. Test-only."""
        self._semaphores.clear()
        self._last_fetch.clear()


__all__ = ["AsyncHostThrottle"]
