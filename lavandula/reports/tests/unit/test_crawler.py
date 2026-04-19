"""AC12.4 — seed URL validation at the trust boundary."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/plain;base64,aGVsbG8=",
        "http://user:p@example.org/",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://localhost/",
        "ftp://example.org/",
        "http://",            # empty hostname
        "not-a-url",
    ],
)
def test_ac12_4_rejects_bad_seed(bad_url):
    from lavandula.reports.crawler import validate_seed_url
    assert not validate_seed_url(bad_url).ok


def test_ac12_4_accepts_plain_https():
    from lavandula.reports.crawler import validate_seed_url
    assert validate_seed_url("https://www.redcross.org/").ok


def test_ac12_4_rejects_bare_ip():
    from lavandula.reports.crawler import validate_seed_url
    assert not validate_seed_url("http://8.8.8.8/").ok
