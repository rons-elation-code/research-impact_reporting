"""Module-level per-host throttle shared across threads (TICK-002).

Worker threads each build their own `ReportsHTTPClient` (because
`requests.Session` is not thread-safe), but politeness is a global
property of the crawler — two threads hitting the same host must still
observe the QPS gap. This module holds the single shared state.

Uses the **reservation pattern**: `reserve(host, now)` atomically
updates the recorded "slot" for `host` BEFORE returning the sleep
duration, so concurrent callers compute correct (sequential) delays
even though the actual `time.sleep` happens outside the lock.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional

from . import config


class HostThrottle:
    """Thread-safe per-host throttle with reservation semantics."""

    def __init__(
        self,
        *,
        min_interval_sec: float | None = None,
        jitter_sec: float | None = None,
    ) -> None:
        self._min_interval = (
            config.REQUEST_DELAY_SEC if min_interval_sec is None else min_interval_sec
        )
        self._jitter = (
            config.REQUEST_DELAY_JITTER_SEC if jitter_sec is None else jitter_sec
        )
        self._lock = threading.Lock()
        self._last_fetch: dict[str, float] = {}

    def reserve(self, host: str, now: float) -> float:
        """Atomically claim the next fetch slot for `host`.

        Returns the duration the caller must `time.sleep()` *outside*
        the lock before issuing its request. The reservation is
        recorded under the lock so concurrent callers serialize.
        """
        with self._lock:
            last = self._last_fetch.get(host)
            if last is None:
                # First-ever request to this host: no wait. Record the
                # slot at `now` so the next caller waits a full interval.
                self._last_fetch[host] = now
                return 0.0
            # S311/B311 OK: jitter is a politeness tweak, not a security primitive.
            jitter = random.uniform(-self._jitter, self._jitter)  # noqa: S311  # nosec B311
            next_allowed = last + self._min_interval + jitter
            wait = max(0.0, next_allowed - now)
            # Reserve the slot NOW so the next caller computes off
            # this reservation, not the as-yet-unslept-to time.
            self._last_fetch[host] = now + wait
            return wait

    def reset(self) -> None:
        """Clear all recorded slots. Test-only."""
        with self._lock:
            self._last_fetch.clear()


_SINGLETON = HostThrottle()


def reserve(host: str, now: float | None = None) -> float:
    """Module-level entry point — workers share this singleton."""
    if now is None:
        now = time.monotonic()
    return _SINGLETON.reserve(host, now)


def reset_for_testing() -> None:
    _SINGLETON.reset()


__all__ = ["HostThrottle", "reserve", "reset_for_testing"]
