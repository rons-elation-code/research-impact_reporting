"""Per-hop redirect policy (AC12.2, AC12.2.1).

Every hop in a redirect chain must be:
  - In the seed's eTLD+1, OR
  - In the hosting-platform allowlist.

Any other intermediate hop triggers `cross_origin_blocked` even if the
final destination would be allowed. Chains longer than
`config.MAX_REDIRECTS` are refused with `redirect_chain_too_long`.

eTLD+1 extraction uses `publicsuffix2` when available, falling back to
a heuristic (rightmost two labels) if the library is missing. The
fallback is the worst case — prefer the installed library.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from . import config


try:
    from publicsuffix2 import get_sld  # type: ignore
    _PSL_AVAILABLE = True
except Exception:  # pragma: no cover
    _PSL_AVAILABLE = False


def etld1(host: str) -> str:
    """Return the registrable domain (eTLD+1) of `host`.

    Uses `publicsuffix2.get_sld` when available. Falls back to the
    rightmost two labels — only safe for `.com/.org/.net`-style TLDs.
    """
    h = (host or "").strip().lower()
    if not h:
        return ""
    if _PSL_AVAILABLE:
        try:
            sld = get_sld(h) or h
        except Exception:
            sld = h
        return sld
    parts = h.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


@dataclass
class RedirectCheckResult:
    ok: bool
    reason: str = ""
    note: str = ""


def _allowed(host: str, seed_etld1: str) -> bool:
    host_e = etld1(host)
    if host_e == seed_etld1:
        return True
    if host_e in config.HOSTING_PLATFORMS:
        return True
    return False


def check_redirect_chain(
    chain: Iterable[str],
    *,
    seed_etld1: str,
) -> RedirectCheckResult:
    """Validate every HOP, not just the final URL.

    Returns `ok=True` iff each URL's host is in {seed eTLD+1} or the
    `HOSTING_PLATFORMS` allowlist.
    """
    urls = list(chain)
    if not urls:
        return RedirectCheckResult(ok=True)
    if len(urls) - 1 > config.MAX_REDIRECTS:
        return RedirectCheckResult(
            ok=False,
            reason="server_error",
            note="redirect_chain_too_long",
        )
    for url in urls:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if not _allowed(host, seed_etld1):
            return RedirectCheckResult(
                ok=False,
                reason="cross_origin_blocked",
                note=f"hop {host!r} not in seed eTLD+1 or platform allowlist",
            )
    return RedirectCheckResult(ok=True)


__all__ = ["etld1", "RedirectCheckResult", "check_redirect_chain"]
