"""Domain and CDX-row validation for Wayback fallback (Spec 0022).

Pure functions covering the two CRITICAL injection paths from the
red-team review:
  - AC15.2: domain validation before CDX URL construction
  - AC15.3: per-row CDX validation before Wayback URL construction
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlsplit

_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_HOSTNAME_MAX_LEN = 253
_TIMESTAMP_RE = re.compile(r"^\d{14}$")
_ORIGINAL_MAX_LEN = 2048


def validate_domain(domain: str) -> Optional[str]:
    """Return the lowercased domain if valid per AC15.2, else None."""
    if not domain or len(domain) > _HOSTNAME_MAX_LEN:
        return None
    domain = domain.lower()
    if not _HOSTNAME_RE.fullmatch(domain):
        return None
    return domain


def validate_cdx_row(row: list) -> Optional[dict]:
    """Validate a CDX response row per AC15.3.

    Expected schema: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
    Extra columns ignored; short rows skipped.
    """
    if not isinstance(row, list) or len(row) < 3:
        return None
    urlkey, timestamp, original = row[0], str(row[1]), str(row[2])
    digest = str(row[5]) if len(row) > 5 else None

    if not _TIMESTAMP_RE.fullmatch(timestamp):
        return None
    if len(original) > _ORIGINAL_MAX_LEN:
        return None

    try:
        parsed = urlsplit(original)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    if validate_domain(parsed.hostname) is None:
        return None

    try:
        cleaned = _strip_credentials_and_fragment(original, parsed)
    except (ValueError, TypeError):
        return None
    if cleaned is None:
        return None

    return {
        "urlkey": urlkey,
        "timestamp": timestamp,
        "original": cleaned,
        "capture_host": parsed.hostname.lower(),
        "digest": digest,
    }


def _strip_credentials_and_fragment(original: str, parsed) -> Optional[str]:
    """Reconstruct the URL without credentials or fragment."""
    hostname = parsed.hostname
    if not hostname:
        return None
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{path}{query}"


def build_wayback_url(timestamp: str, original: str) -> str:
    """Construct the id_-modifier raw-bytes Wayback URL per AC8."""
    safe_chars = ":/?#[]@!$&'()*+,;="
    return f"https://web.archive.org/web/{timestamp}id_/{quote(original, safe=safe_chars)}"


def build_cdx_url(domain: str) -> Optional[str]:
    """Build the CDX query URL with strict domain validation (AC15.2)."""
    validated = validate_domain(domain)
    if validated is None:
        return None
    encoded = quote(validated, safe="")
    return (
        f"https://web.archive.org/cdx/search/cdx?"
        f"url={encoded}/*&"
        f"matchType=domain&"
        f"filter=mimetype:application/pdf&"
        f"filter=statuscode:200&"
        f"output=json&"
        f"limit=500"
    )


__all__ = [
    "validate_domain",
    "validate_cdx_row",
    "build_wayback_url",
    "build_cdx_url",
]
