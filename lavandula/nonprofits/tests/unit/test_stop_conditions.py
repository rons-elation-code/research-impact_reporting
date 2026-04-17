"""Unit tests for stop-condition detector."""
from lavandula.nonprofits import config
from lavandula.nonprofits.stop_conditions import StopConditions


def test_403_halt(tmp_path):
    s = StopConditions(archive_root=tmp_path, runtime_free_gb=0)
    for _ in range(config.MAX_CONSECUTIVE_403):
        s.observe_fetch("forbidden")
    assert s.evaluate() == "consecutive_403"


def test_429_halt(tmp_path):
    s = StopConditions(archive_root=tmp_path, runtime_free_gb=0)
    for _ in range(config.MAX_CONSECUTIVE_429):
        s.observe_fetch("rate_limited")
    assert s.evaluate() == "consecutive_429"


def test_ok_resets_403(tmp_path):
    s = StopConditions(archive_root=tmp_path, runtime_free_gb=0)
    s.observe_fetch("forbidden")
    s.observe_fetch("forbidden")
    s.observe_fetch("ok")
    s.observe_fetch("forbidden")
    assert s.evaluate() == ""  # counter reset


def test_challenge_halt_immediate(tmp_path):
    s = StopConditions(archive_root=tmp_path, runtime_free_gb=0)
    s.observe_fetch("challenge")
    assert s.evaluate() == "challenge_detected"


def test_long_retry_after_halt(tmp_path):
    s = StopConditions(archive_root=tmp_path, runtime_free_gb=0)
    s.observe_fetch("rate_limited", retry_after=400)
    s.observe_fetch("rate_limited", retry_after=400)
    assert s.evaluate() == "long_retry_after"


def test_archive_cap_halt(tmp_path):
    s = StopConditions(archive_root=tmp_path, max_archive_gb=1)
    # Simulate 2 GB of cumulative ok bytes.
    for _ in range(2):
        s.observe_fetch("ok", bytes_read=1024 * 1024 * 1024)
    assert s.evaluate() == "archive_cap"
