"""robots.txt parser with spec 0001's stanza-matching semantics.

Policy (from spec § Security Considerations / robots.txt policy):
  - Find the MOST SPECIFIC `User-agent:` stanza whose token is a case-
    insensitive substring of our configured UA. Specificity = token length.
  - If multiple stanzas tie in specificity → AmbiguousRobots (halt).
  - If no match → fall back to `User-agent: *`.
  - Any parse error → halt.
  - Hardcoded DISALLOWED_EINS are floor, not ceiling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from . import config


class RobotsError(RuntimeError):
    """Any robots.txt problem that should halt the crawl."""


class AmbiguousRobots(RobotsError):
    """Two UA stanzas tied on specificity."""


@dataclass
class _Stanza:
    agent_tokens: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)  # (directive, value)


@dataclass
class RobotsPolicy:
    """Compiled robots.txt policy for a specific UA.

    `disallow_patterns` holds the raw pattern strings applicable to our UA
    (most-specific stanza or the wildcard fallback).
    """
    ua: str
    disallow_patterns: list[str] = field(default_factory=list)
    allow_patterns: list[str] = field(default_factory=list)
    matched_agent: str = ""

    def is_allowed(self, path: str) -> bool:
        """Return True if the path is allowed under the compiled policy.

        More-specific (longer) allow overrides disallow (RFC 9309 §2.2.2).
        """
        best_allow = -1
        best_disallow = -1
        for pat in self.allow_patterns:
            if _pattern_matches(pat, path):
                best_allow = max(best_allow, len(pat))
        for pat in self.disallow_patterns:
            if _pattern_matches(pat, path):
                best_disallow = max(best_disallow, len(pat))
        if best_disallow < 0:
            return True
        return best_allow >= best_disallow


def _pattern_matches(pattern: str, path: str) -> bool:
    """Match a robots.txt path pattern against a URL path.

    Empty Disallow means 'allow all' (which means the pattern doesn't
    match). Supports `*` wildcard and `$` end-of-string per RFC 9309.
    """
    if pattern == "":
        return False
    # Build a regex: escape, then replace * and $.
    regex = re.escape(pattern)
    regex = regex.replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return re.match(regex, path) is not None


def parse(text: str, *, ua: str) -> RobotsPolicy:
    """Parse robots.txt text and compile a RobotsPolicy for `ua`.

    Raises RobotsError on parse failure or AmbiguousRobots on tied
    specificity.
    """
    stanzas: list[_Stanza] = []
    current: _Stanza | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            # Blank line ends the current stanza.
            if current is not None and (current.agent_tokens or current.rules):
                stanzas.append(current)
                current = None
            continue
        if ":" not in line:
            raise RobotsError(f"malformed robots.txt line: {raw_line!r}")
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if current is None or current.rules:
                # Starting a new stanza (or extending agent tokens of a
                # fresh one).
                if current is not None:
                    stanzas.append(current)
                current = _Stanza()
            current.agent_tokens.append(value)
        elif directive in ("disallow", "allow"):
            if current is None:
                current = _Stanza()
            current.rules.append((directive, value))
        else:
            # Sitemap, Crawl-delay, etc. — ignore for now.
            if current is None:
                current = _Stanza()
    if current is not None and (current.agent_tokens or current.rules):
        stanzas.append(current)

    # Find best stanza by UA substring match.
    ua_lower = ua.lower()
    best_len = -1
    best_matches: list[_Stanza] = []
    wildcard_stanza: _Stanza | None = None
    matched_token = ""
    for st in stanzas:
        for token in st.agent_tokens:
            t = token.strip()
            if t == "*":
                wildcard_stanza = st
                continue
            if t and t.lower() in ua_lower:
                if len(t) > best_len:
                    best_len = len(t)
                    best_matches = [st]
                    matched_token = t
                elif len(t) == best_len:
                    best_matches.append(st)
    if len(best_matches) > 1:
        raise AmbiguousRobots(
            f"multiple robots.txt stanzas tie in specificity ({best_len})"
        )
    chosen: _Stanza | None
    if best_matches:
        chosen = best_matches[0]
    elif wildcard_stanza is not None:
        chosen = wildcard_stanza
        matched_token = "*"
    else:
        chosen = None
        matched_token = ""

    disallow: list[str] = []
    allow: list[str] = []
    if chosen is not None:
        for directive, value in chosen.rules:
            if directive == "disallow":
                if value != "":  # empty Disallow = allow all
                    disallow.append(value)
            elif directive == "allow":
                if value != "":
                    allow.append(value)
    return RobotsPolicy(
        ua=ua,
        disallow_patterns=disallow,
        allow_patterns=allow,
        matched_agent=matched_token,
    )


def is_ein_disallowed(ein: str, policy: RobotsPolicy) -> bool:
    """True if `ein` is in the hardcoded floor OR matches policy disallow."""
    from .url_utils import canonicalize_ein
    try:
        c = canonicalize_ein(ein)
    except ValueError:
        return True
    if c in config.DISALLOWED_EINS:
        return True
    path = f"/ein/{c}"
    return not policy.is_allowed(path)


def allows_ein_path(policy: RobotsPolicy) -> bool:
    """Sanity check: does this policy still permit /ein/* overall?

    The spec halts the crawl if robots.txt starts disallowing /ein/*.
    """
    # Explicit probe.
    sample = "/ein/530196605"
    return policy.is_allowed(sample)
