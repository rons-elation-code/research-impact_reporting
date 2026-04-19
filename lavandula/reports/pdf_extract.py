"""In-process PDF helpers that don't need the sandbox (AC15, AC18.2).

The full first-page-text + metadata extraction runs inside the
`sandbox/` runner — this module holds the deterministic, cheap helpers
(active-content byte scan and metadata sanitization) that both the
sandbox CHILD and the parent use.

AC15 — active-content detection: scan PDF bytes for raw markers.
    Scanning bytes (rather than parsing the object graph) is a
    defense-in-depth cross-check. False positives are acceptable
    (the flag is recorded, not rejected); false negatives are the
    concern we're guarding against.

AC18.2 — metadata sanitization: `pdf_creator` and `pdf_producer`
    pass through the log-sanitizer (strip ANSI, control chars,
    zero-width) and truncate to 200 chars BEFORE DB insert.
"""
from __future__ import annotations

import re
from typing import Optional

from .logging_utils import sanitize

_MAX_METADATA_LEN = 200

# Match PDF tokens at word boundaries. The `/JavaScript`, `/JS`, `/Launch`,
# `/EmbeddedFile`, `/URI` tokens are all standard CSE names.
_RE_JS = re.compile(rb"/(?:JavaScript|JS)\b")
_RE_LAUNCH = re.compile(rb"/Launch\b")
_RE_EMBEDDED = re.compile(rb"/EmbeddedFile\b")
_RE_URI = re.compile(rb"/URI\b")


def scan_active_content(pdf_bytes: bytes) -> dict[str, int]:
    """Return dict with 0/1 flags for JS / Launch / EmbeddedFile / URI actions."""
    return {
        "pdf_has_javascript": 1 if _RE_JS.search(pdf_bytes) else 0,
        "pdf_has_launch": 1 if _RE_LAUNCH.search(pdf_bytes) else 0,
        "pdf_has_embedded": 1 if _RE_EMBEDDED.search(pdf_bytes) else 0,
        "pdf_has_uri_actions": 1 if _RE_URI.search(pdf_bytes) else 0,
    }


# Strip ANSI escapes, BiDi overrides, zero-width, byte-order marks, NULs.
_NOISE_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences
)


def sanitize_metadata_field(value: Optional[str]) -> Optional[str]:
    """Scrub control chars / ANSI / zero-width and cap at 200 chars.

    `None` passes through unchanged so SQLite stores NULL.
    """
    if value is None:
        return None
    # First pass: strip noise (ANSI / control / zero-width).
    cleaned = _NOISE_RE.sub("", value)
    # Second pass: run through the log sanitizer for any remaining controls.
    cleaned = sanitize(cleaned, max_len=_MAX_METADATA_LEN)
    # The log sanitizer may append '...<truncated>'; enforce the hard cap.
    if len(cleaned) > _MAX_METADATA_LEN:
        cleaned = cleaned[:_MAX_METADATA_LEN]
    return cleaned


__all__ = ["scan_active_content", "sanitize_metadata_field"]
