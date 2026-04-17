"""Central stop-condition tracker.

Each `observe_*` call updates internal counters. `evaluate` returns a
halt reason string (non-empty = halt now) or empty string (continue).
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class StopConditionState:
    started_at: float = field(default_factory=time.monotonic)
    consecutive_403: int = 0
    consecutive_429: int = 0
    consecutive_long_retry_after: int = 0
    cumulative_archive_bytes: int = 0
    saw_challenge: bool = False
    robots_disallowed: bool = False
    disk_low: bool = False
    halt_reason: str = ""


class StopConditions:
    """Stateful stop-condition detector wired to the crawler loop."""

    def __init__(
        self,
        *,
        archive_root: Path,
        runtime_free_gb: int | None = None,
        max_archive_gb: int | None = None,
        max_runtime_hours: float | None = None,
    ) -> None:
        self.state = StopConditionState()
        self.archive_root = archive_root
        self.runtime_free_gb = (
            runtime_free_gb if runtime_free_gb is not None else config.RUNTIME_FREE_GB
        )
        self.max_archive_gb = (
            max_archive_gb if max_archive_gb is not None else config.MAX_ARCHIVE_GB
        )
        self.max_runtime_hours = (
            max_runtime_hours if max_runtime_hours is not None else config.MAX_RUNTIME_HOURS
        )

    # --- Observers ------------------------------------------------------

    def observe_fetch(self, fetch_status: str, *, retry_after: float | None = None,
                      bytes_read: int = 0) -> None:
        if fetch_status == "forbidden":
            self.state.consecutive_403 += 1
        else:
            self.state.consecutive_403 = 0

        if fetch_status == "rate_limited":
            self.state.consecutive_429 += 1
        elif fetch_status == "ok":
            self.state.consecutive_429 = 0

        if retry_after is not None and retry_after > config.MAX_RETRY_AFTER_SEC:
            self.state.consecutive_long_retry_after += 1
        elif retry_after is not None:
            self.state.consecutive_long_retry_after = 0

        if fetch_status == "challenge":
            self.state.saw_challenge = True

        if fetch_status == "ok":
            self.state.cumulative_archive_bytes += max(0, bytes_read)

    def observe_robots_disallow(self) -> None:
        self.state.robots_disallowed = True

    # --- Evaluator ------------------------------------------------------

    def evaluate(self) -> str:
        s = self.state
        if s.halt_reason:
            return s.halt_reason
        if s.robots_disallowed:
            s.halt_reason = "robots_disallowed"
            return s.halt_reason
        if s.saw_challenge:
            s.halt_reason = "challenge_detected"
            return s.halt_reason
        if s.consecutive_403 >= config.MAX_CONSECUTIVE_403:
            s.halt_reason = "consecutive_403"
            return s.halt_reason
        if s.consecutive_429 >= config.MAX_CONSECUTIVE_429:
            s.halt_reason = "consecutive_429"
            return s.halt_reason
        if s.consecutive_long_retry_after >= config.MAX_CONSECUTIVE_LONG_RETRY_AFTER:
            s.halt_reason = "long_retry_after"
            return s.halt_reason
        elapsed_h = (time.monotonic() - s.started_at) / 3600.0
        if elapsed_h > self.max_runtime_hours:
            s.halt_reason = "runtime_exceeded"
            return s.halt_reason
        if s.cumulative_archive_bytes > self.max_archive_gb * (1024 ** 3):
            s.halt_reason = "archive_cap"
            return s.halt_reason
        # Disk-space check (runtime).
        try:
            free_bytes = shutil.disk_usage(self.archive_root).free
        except OSError:
            free_bytes = None
        if free_bytes is not None and free_bytes < self.runtime_free_gb * (1024 ** 3):
            s.halt_reason = "disk_low"
            return s.halt_reason
        return ""


def preflight_disk_check(archive_root: Path, *, min_gb: int | None = None) -> tuple[bool, int]:
    """Return (ok, free_gb) for the archive partition."""
    min_gb = min_gb or config.PREFLIGHT_FREE_GB
    try:
        free_bytes = shutil.disk_usage(archive_root).free
    except OSError:
        return False, 0
    free_gb = free_bytes // (1024 ** 3)
    return free_gb >= min_gb, free_gb
