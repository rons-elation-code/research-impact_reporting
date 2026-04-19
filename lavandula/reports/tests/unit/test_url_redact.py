"""AC13 — URL redaction; AC25 — URL canonicalization.

Both run on every URL before it hits the DB.
"""
from __future__ import annotations

import pytest


def test_ac13_roundtrip_userinfo_query_fragment():
    from lavandula.reports.url_redact import redact_url
    inp = "https://u:p@host.org/x?api_key=AAA&normal=ok#access_token=BBB"
    out = redact_url(inp)
    assert "u:p@" not in out
    assert "api_key=REDACTED" in out
    assert "normal=ok" in out
    assert "access_token=REDACTED" in out
    assert "BBB" not in out
    assert "AAA" not in out


@pytest.mark.parametrize(
    "param",
    [
        "token",
        "api_key",
        "apikey",
        "api-key",
        "access_token",
        "access-token",
        "refresh_token",
        "id_token",
        "bearer",
        "password",
        "pwd",
        "secret",
        "credential",
        "sig",
        "signature",
        "code",
        "key",
        "auth",
        "session",
    ],
)
def test_ac13_every_sensitive_param_redacted(param):
    from lavandula.reports.url_redact import redact_url
    out = redact_url(f"https://example.org/x?{param}=sekret&ok=v")
    assert "sekret" not in out
    assert f"{param}=REDACTED" in out.lower()
    assert "ok=v" in out


def test_ac13_case_insensitive():
    from lavandula.reports.url_redact import redact_url
    assert "sekret" not in redact_url("https://x.org/a?API_KEY=sekret")
    assert "sekret" not in redact_url("https://x.org/a?Access_Token=sekret")


def test_ac25_lowercase_scheme_host_strip_default_port():
    from lavandula.reports.url_redact import canonicalize_url
    assert canonicalize_url("HTTPS://Example.ORG:443/a/") == "https://example.org/a"
    assert canonicalize_url("HTTP://Example.ORG:80/") == "http://example.org/"


def test_ac25_trailing_slash_preserved_for_root():
    from lavandula.reports.url_redact import canonicalize_url
    assert canonicalize_url("https://example.org/") == "https://example.org/"


def test_ac25_idn_punycoded():
    from lavandula.reports.url_redact import canonicalize_url
    out = canonicalize_url("https://bücher.example/")
    assert "xn--bcher-kva" in out


def test_ac25_query_params_sorted():
    from lavandula.reports.url_redact import canonicalize_url
    a = canonicalize_url("https://x.org/p?b=2&a=1")
    b = canonicalize_url("https://x.org/p?a=1&b=2")
    assert a == b


def test_ac25_fragment_removed():
    from lavandula.reports.url_redact import canonicalize_url
    assert "#" not in canonicalize_url("https://x.org/a#frag")
