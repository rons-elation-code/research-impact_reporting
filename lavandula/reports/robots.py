"""robots.txt compliance (AC1) with 'no carve-out for the wildcard'.

Standard RFC robots.txt (and stdlib `urllib.robotparser`) use
closest-match-wins-fully semantics: if a specific-UA stanza matches,
the wildcard is entirely ignored. Spec AC1 explicitly overrides this:

  Disallow rules under `User-agent: *` are honored unless a
  more-specific stanza matching our identifiable UA explicitly
  permits the path.

So we parse stanzas ourselves and apply:
  1. Compute the wildcard decision (* stanza only).
  2. Compute the specific-UA decision, if present.
  3. Disallowed ⇔ wildcard Disallows AND specific-UA does not
     explicitly Allow.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

from . import config


@dataclasses.dataclass
class _Rule:
    allow: bool
    path: str  # raw pattern (may include * and $)


@dataclasses.dataclass
class _Stanza:
    agents: list[str]
    rules: list[_Rule]


def _parse_stanzas(text: str) -> list[_Stanza]:
    stanzas: list[_Stanza] = []
    current: _Stanza | None = None
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            # Blank line ends the current stanza if it has rules.
            if current is not None and current.rules:
                stanzas.append(current)
                current = None
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "user-agent":
            if current is None or current.rules:
                if current is not None and current.rules:
                    stanzas.append(current)
                current = _Stanza(agents=[val], rules=[])
            else:
                current.agents.append(val)
        elif key == "disallow":
            if current is None:
                continue
            if val:
                current.rules.append(_Rule(allow=False, path=val))
            # Empty Disallow means 'nothing disallowed' — intentionally ignored
            # as a rule (its absence means allowed-by-default).
        elif key == "allow":
            if current is None:
                continue
            if val:
                current.rules.append(_Rule(allow=True, path=val))
    if current is not None and current.rules:
        stanzas.append(current)
    return stanzas


def _pattern_matches(pattern: str, path: str) -> bool:
    """Match robots.txt style: prefix + `*` wildcards + optional `$`."""
    if pattern == "":
        return False
    anchor_end = pattern.endswith("$")
    pat = pattern[:-1] if anchor_end else pattern
    parts = pat.split("*")
    i = 0
    first = True
    for p in parts:
        if first:
            if not path.startswith(p):
                return False
            i = len(p)
            first = False
        else:
            if not p:
                continue
            j = path.find(p, i)
            if j < 0:
                return False
            i = j + len(p)
    if anchor_end:
        return i == len(path)
    return True


def _stanza_decision(stanza: _Stanza, path: str) -> bool | None:
    """Return True=allow, False=disallow, None=no opinion.

    Longest matching rule wins; ties go to Allow (RFC-ish).
    """
    best: tuple[int, bool] | None = None
    for rule in stanza.rules:
        if _pattern_matches(rule.path, path):
            plen = len(rule.path.rstrip("$"))
            if best is None or plen > best[0] or (plen == best[0] and rule.allow):
                best = (plen, rule.allow)
    if best is None:
        return None
    return best[1]


def _ua_matches(stanza_agent: str, my_ua: str) -> bool:
    """Spec AC1: stanza UA is 'specific to us' iff our UA contains it."""
    s = stanza_agent.strip()
    if s == "*":
        return False
    return s.lower() in my_ua.lower()


def sitemap_urls_from_robots(robots_text: str) -> list[str]:
    """Extract `Sitemap: <url>` directives from robots.txt.

    TICK-004 AC2: ALL directives are returned in order, so the caller
    can attempt each one (failures are individual, not fatal).
    Case-insensitive key match per RFC 9309. Blank/comment lines
    ignored. Returns [] if no Sitemap: directives present.
    """
    urls: list[str] = []
    for raw_line in (robots_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() != "sitemap":
            continue
        url = value.strip()
        if url:
            urls.append(url)
    return urls


def can_fetch(
    robots_text: str,
    path: str,
    *,
    user_agent: str = config.USER_AGENT,
) -> bool:
    """Return True iff `user_agent` may fetch `path`.

    Semantics per AC1(c):
      - Wildcard stanza decides the baseline.
      - A specific-UA stanza can ONLY explicitly Allow a path to
        override a wildcard Disallow; it cannot broaden access beyond
        that nor silently ignore the wildcard.
    """
    # Normalize `path` to just the path+query if a URL was passed.
    if path.startswith(("http://", "https://")):
        sp = urlsplit(path)
        p = sp.path or "/"
        if sp.query:
            p = p + "?" + sp.query
        path = p
    if not path.startswith("/"):
        path = "/" + path

    stanzas = _parse_stanzas(robots_text)
    wildcard_decision: bool | None = None
    specific_decision: bool | None = None
    for stanza in stanzas:
        if any(a.strip() == "*" for a in stanza.agents):
            d = _stanza_decision(stanza, path)
            if d is not None:
                wildcard_decision = d
        if any(_ua_matches(a, user_agent) for a in stanza.agents):
            d = _stanza_decision(stanza, path)
            if d is not None:
                specific_decision = d

    # Baseline: if nothing disallows, allow.
    allowed = wildcard_decision is not False
    # Specific Allow can lift a wildcard Disallow.
    if specific_decision is True:
        allowed = True
    # Specific Disallow locks down even if wildcard allows.
    elif specific_decision is False:
        allowed = False
    return allowed


@dataclasses.dataclass
class _CacheEntry:
    text: str
    fetched_at: float


class RobotsCache:
    """Per-host robots.txt cache with 24h TTL (config.ROBOTS_CACHE_TTL_SEC).

    The fetcher callback receives a host and returns the raw robots.txt
    text (or None on 404; treated as 'no restrictions').
    """

    def __init__(
        self,
        fetcher: Callable[[str], str | None],
        *,
        now: Callable[[], float] = time.monotonic,
        ttl_sec: int = config.ROBOTS_CACHE_TTL_SEC,
    ) -> None:
        self._fetcher = fetcher
        self._now = now
        self._ttl = ttl_sec
        self._cache: dict[str, _CacheEntry] = {}

    def get_text(self, host: str) -> str:
        entry = self._cache.get(host)
        now = self._now()
        if entry is not None and (now - entry.fetched_at) < self._ttl:
            return entry.text
        text = self._fetcher(host) or ""
        self._cache[host] = _CacheEntry(text=text, fetched_at=now)
        return text

    def can_fetch(
        self,
        host: str,
        path: str,
        *,
        user_agent: str = config.USER_AGENT,
    ) -> bool:
        text = self.get_text(host)
        return can_fetch(text, path, user_agent=user_agent)


__all__ = ["can_fetch", "RobotsCache"]
