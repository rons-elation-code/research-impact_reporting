"""Phone number extraction from text (Spec 0031).

Extracts US phone numbers from search snippets and web page text,
filtering out fax numbers and toll-free numbers.
"""
from __future__ import annotations

import re

_US_PHONE_RE = re.compile(
    r'(?:\+?1[-.\s]?)?'
    r'\(?(\d{3})\)?[-.\s]'
    r'(\d{3})[-.\s]'
    r'(\d{4})'
    r'(?!\d)'
)

_FAX_CONTEXT_RE = re.compile(r'\bfax\b', re.I)

_TOLLFREE_PREFIXES = frozenset({"800", "888", "877", "866", "855", "844", "833"})


def extract_phone(
    text: str,
    *,
    allow_tollfree: bool = False,
    org_name: str = "",
) -> str | None:
    """Extract best valid US phone number from text.

    Returns normalized (XXX) XXX-XXXX string, or None if no valid phone found.
    """
    if not text:
        return None

    candidates: list[tuple[str, int]] = []

    for match in _US_PHONE_RE.finditer(text):
        area, exchange, subscriber = match.groups()
        pos = match.start()

        # Reject fax numbers: check 20-char window before match
        window_start = max(0, pos - 20)
        before_text = text[window_start:pos]
        if _FAX_CONTEXT_RE.search(before_text):
            continue

        # Reject toll-free unless allowed
        if area in _TOLLFREE_PREFIXES and not allow_tollfree:
            continue

        formatted = f"({area}) {exchange}-{subscriber}"
        candidates.append((formatted, pos))

    if not candidates:
        return None

    if len(candidates) == 1 or not org_name:
        return candidates[0][0]

    # Prefer phone closest to org name in text
    org_lower = org_name.lower()
    text_lower = text.lower()
    org_positions = []
    start = 0
    while True:
        idx = text_lower.find(org_lower, start)
        if idx == -1:
            break
        org_positions.append(idx)
        start = idx + 1

    if not org_positions:
        return candidates[0][0]

    def distance_to_org(phone_pos: int) -> int:
        return min(abs(phone_pos - op) for op in org_positions)

    candidates.sort(key=lambda c: distance_to_org(c[1]))
    return candidates[0][0]


__all__ = [
    "extract_phone",
]
