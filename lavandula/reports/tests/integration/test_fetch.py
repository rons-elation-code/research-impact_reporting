"""AC6 — per-host 3s throttle + jitter."""
from __future__ import annotations

import time
import pytest


def test_ac6_throttle_takes_at_least_25s_for_10_requests():
    """10 back-to-back fetches to the same host request sleep totalling ≥ 22.5s
    (9 gaps of ≥ 2.5s each: 3s delay ± 0.5s jitter).

    Runs with a FIXED monotonic clock so the throttle engages on every
    call after the first — tests the throttle's intent, not wall time.
    """
    from lavandula.reports.http_client import ReportsHTTPClient
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)

    client = ReportsHTTPClient(
        allow_insecure_cleartext=True,
        sleep=fake_sleep,
        monotonic=lambda: 0.0,
    )
    client.tick_throttle("example.org")
    for _ in range(9):
        client.tick_throttle("example.org")
    total = sum(slept)
    # 9 gaps at ≥ 2.5s each (3s delay - 0.5s jitter worst case)
    assert total >= 22.5
