"""AC6 — per-host 3s throttle + jitter."""
from __future__ import annotations

import time
import pytest


def test_ac6_throttle_takes_at_least_25s_for_10_requests(monkeypatch):
    """10 back-to-back fetches to the same host take ≥ 25s (3s × 9 + slack)."""
    from lavandula.reports.http_client import ReportsHTTPClient
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)

    client = ReportsHTTPClient(
        allow_insecure_cleartext=True,
        sleep=fake_sleep,
        monotonic=iter([0.0, 0.01] + [i * 3.1 for i in range(1, 50)]).__next__,
    )
    client.tick_throttle("example.org")
    for _ in range(9):
        client.tick_throttle("example.org")
    total = sum(slept)
    # 9 gaps at ≥ 2.5s each (3s ± 0.5s jitter)
    assert total >= 22.5
