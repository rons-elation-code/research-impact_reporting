"""URL redaction (AC13) and canonicalization (AC25).

Both run on every URL before it is written to any DB column. The two are
composed in `redact_and_canonicalize` — the canonical form is the one we
dedup on, and the redacted form is what appears in logs + `reports.*`.

Per the AC13 expansion:
- Sensitive query-params (case-insensitive) → `REDACTED`.
- `userinfo` (user:pass@) stripped unconditionally.
- Fragment scanned for sensitive key=value pairs; those are replaced
  with `REDACTED`.
"""
from __future__ import annotations

import re
import urllib.parse as _up

from . import config


_SENSITIVE = frozenset(p.lower() for p in config.SENSITIVE_URL_PARAMS)


def _redact_qs(query: str) -> str:
    if not query:
        return ""
    parts = _up.parse_qsl(query, keep_blank_values=True)
    redacted: list[tuple[str, str]] = []
    for k, v in parts:
        if k.lower() in _SENSITIVE:
            redacted.append((k, "REDACTED"))
        else:
            redacted.append((k, v))
    return _up.urlencode(redacted, doseq=True)


# Matches the kinds of fragment segments that commonly carry tokens in
# OAuth implicit flows / JWTs posted in fragments.
_FRAGMENT_RE = re.compile(
    r"(?i)\b("
    + "|".join(re.escape(p) for p in sorted(_SENSITIVE, key=len, reverse=True))
    + r")=([^&]*)"
)


def _redact_fragment(fragment: str) -> str:
    if not fragment:
        return ""
    return _FRAGMENT_RE.sub(lambda m: f"{m.group(1)}=REDACTED", fragment)


def redact_url(url: str) -> str:
    """Return a sanitized version of `url`.

    Strips userinfo, redacts sensitive query + fragment params.
    """
    parsed = _up.urlsplit(url)
    # Strip userinfo by reconstructing netloc from host + port.
    host = parsed.hostname or ""
    netloc = host
    if parsed.port is not None:
        netloc = f"{host}:{parsed.port}"
    query = _redact_qs(parsed.query)
    fragment = _redact_fragment(parsed.fragment)
    return _up.urlunsplit(
        (parsed.scheme, netloc, parsed.path, query, fragment)
    )


def canonicalize_url(url: str) -> str:
    """AC25 canonicalization:
      (a) lowercase scheme;
      (b) lowercase host + IDN-punycode;
      (c) strip default ports (:80 / :443);
      (d) remove fragment;
      (e) trim trailing / on non-root paths;
      (f) sort query params (stable dedup).

    URL redaction is applied separately via `redact_url`; combine via
    `redact_and_canonicalize`.
    """
    parsed = _up.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    if host:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            # Already ASCII or malformed — fall back to lowercase.
            host = host.lower()
        host = host.lower()
    port = parsed.port
    if port is not None and (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        port = None
    netloc = host + (f":{port}" if port is not None else "")

    path = parsed.path or ""
    if path and path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Sort query params for deterministic dedup.
    if parsed.query:
        pairs = _up.parse_qsl(parsed.query, keep_blank_values=True)
        pairs.sort()
        query = _up.urlencode(pairs, doseq=True)
    else:
        query = ""

    return _up.urlunsplit((scheme, netloc, path, query, ""))


def redact_and_canonicalize(url: str) -> str:
    """Canonical form with sensitive params redacted."""
    return canonicalize_url(redact_url(url))


__all__ = ["redact_url", "canonicalize_url", "redact_and_canonicalize"]
