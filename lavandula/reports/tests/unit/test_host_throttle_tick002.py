"""TICK-002 — module-level HostThrottle singleton + reservation pattern."""
from __future__ import annotations

import threading
import time

import pytest

from lavandula.reports import host_throttle
from lavandula.reports.host_throttle import HostThrottle


@pytest.fixture(autouse=True)
def _reset_singleton():
    host_throttle.reset_for_testing()
    yield
    host_throttle.reset_for_testing()


def test_reservation_updates_last_fetch_before_returning():
    """Two back-to-back reserves with no monotonic advance must return
    a positive wait on the second call (proves the slot was reserved)."""
    th = HostThrottle(min_interval_sec=3.0, jitter_sec=0.0)
    wait1 = th.reserve("host.example.org", now=0.0)
    wait2 = th.reserve("host.example.org", now=0.0)
    assert wait1 == 0.0
    assert wait2 >= 3.0


def test_concurrent_reserves_same_host_compute_sequential_delays():
    """N threads each call reserve("h", now=0). Sleep budget grows
    monotonically — proves reservation pattern is atomic."""
    th = HostThrottle(min_interval_sec=2.0, jitter_sec=0.0)
    waits: list[float] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        # All threads pass `now=0.0` so the only thing that can stagger
        # them is the reservation update inside the singleton's lock.
        w = th.reserve("h", now=0.0)
        with lock:
            waits.append(w)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    waits.sort()
    # First call waits 0, then each subsequent call waits ≥ prior + 2s.
    assert waits[0] == 0.0
    for prev, cur in zip(waits, waits[1:]):
        assert cur >= prev + 1.999, (waits, prev, cur)


def test_different_hosts_do_not_block_each_other():
    th = HostThrottle(min_interval_sec=5.0, jitter_sec=0.0)
    assert th.reserve("a.example.org", now=0.0) == 0.0
    assert th.reserve("b.example.org", now=0.0) == 0.0
    assert th.reserve("c.example.org", now=0.0) == 0.0


def test_module_singleton_shared_across_callers():
    """Two distinct ReportsHTTPClient-like calls share the singleton."""
    host_throttle.reset_for_testing()
    w1 = host_throttle.reserve("shared.example.org", now=0.0)
    w2 = host_throttle.reserve("shared.example.org", now=0.0)
    assert w1 == 0.0
    assert w2 > 0.0
