"""Small EIN helpers shared across modules.

The 10-rule website URL normalization pipeline lives in url_normalize.py.
"""
from __future__ import annotations

import re

_EIN_RE = re.compile(r"^[0-9]{9}$")
_EIN_ANY_RE = re.compile(r"^[0-9]{2}-?[0-9]{7}$")


def canonicalize_ein(value: str) -> str:
    """Strip dashes and validate an EIN is exactly 9 ASCII digits.

    Raises ValueError on anything else. This is the ONLY place EINs
    acquire filesystem-safety: callers downstream treat the return value
    as trusted.
    """
    if not isinstance(value, str):
        raise ValueError(f"EIN must be str, got {type(value).__name__}")
    stripped = value.strip().replace("-", "")
    if not _EIN_RE.match(stripped):
        raise ValueError(f"invalid EIN: {value!r}")
    return stripped


def ein_from_profile_url(url: str) -> str | None:
    """Extract an EIN from a `/ein/{ein}` profile URL. Returns None if not."""
    m = re.search(r"/ein/([0-9]{9})(?:[/?#]|$)", url or "")
    if not m:
        return None
    return m.group(1)
