"""Unit tests for Retry-After parsing, throttle, Content-Length sanity."""
from lavandula.nonprofits.http_client import parse_retry_after


def test_retry_after_seconds():
    assert parse_retry_after("120") == 120.0


def test_retry_after_zero():
    assert parse_retry_after("0") == 0.0


def test_retry_after_http_date_future():
    # HTTP-date in the future → positive seconds.
    from email.utils import formatdate
    import time
    future = formatdate(time.time() + 30, usegmt=True)
    result = parse_retry_after(future)
    assert result is not None
    assert 0 < result <= 35


def test_retry_after_empty():
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


def test_retry_after_negative_clamped():
    assert parse_retry_after("-10") == 0.0
