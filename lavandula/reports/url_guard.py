"""SSRF guard (AC12) + DNS IP pinning (AC12.1).

`is_address_allowed` rejects RFC-class private/loopback/link-local/
multicast/reserved/unspecified plus a named cloud-metadata deny list,
across both IPv4 and IPv6. `::ffff:IPv4`-mapped addresses normalize to
their IPv4 form before the check.

`HostPinCache` resolves a hostname once per crawl session. Subsequent
lookups return the pinned IP so a rebinding DNS response cannot flip
the target mid-session. The cache also checks the pinned IP against
`is_address_allowed` at pin time — a rebind that first answers public
then later answers private is refused at pin time (the first public IP
is used for the session duration, not re-checked).
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass
from typing import Callable

from . import config


Resolver = Callable[[str], str]


def _normalize(ip: str) -> ipaddress._BaseAddress:
    addr = ipaddress.ip_address(ip)
    # Normalize IPv4-mapped IPv6 to IPv4 to avoid bypasses like ::ffff:10.0.0.1.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_address_allowed(ip: str) -> bool:
    """True iff `ip` is a publicly-routable address we're willing to fetch.

    Rejects:
      - Named cloud-metadata addresses (AWS/Azure/Alibaba + v6 variants).
      - Every RFC-class private / loopback / link-local / multicast /
        reserved / unspecified block.
      - IPv4-mapped-IPv6 forms of the above.
    """
    if ip in config.CLOUD_METADATA_DENY:
        return False
    try:
        addr = _normalize(ip)
    except ValueError:
        return False
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    return True


def _default_resolver(host: str) -> str:
    """Resolve host → first usable IPv4/IPv6 address.

    Uses `getaddrinfo` and returns the first result; callers may swap in
    a test resolver via `HostPinCache(resolver=...)`.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    if not infos:
        raise socket.gaierror(f"no address for {host}")
    return infos[0][4][0]


class DNSResolutionError(RuntimeError):
    """Host could not be resolved, or resolved to a disallowed address."""


@dataclass
class HostPinCache:
    """Per-session DNS pin cache (AC12.1)."""

    resolver: Resolver = _default_resolver
    _pins: dict[str, str] = None  # type: ignore[assignment]
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._pins = {}
        self._lock = threading.Lock()

    def pin(self, host: str) -> str:
        """Return the pinned IP for `host`. Resolves on first access."""
        host_key = host.lower()
        with self._lock:
            cached = self._pins.get(host_key)
            if cached is not None:
                return cached
            ip = self.resolver(host)
            if not is_address_allowed(ip):
                raise DNSResolutionError(
                    f"resolved {host_key!r} -> {ip!r} (disallowed)"
                )
            self._pins[host_key] = ip
            return ip

    def clear(self) -> None:
        with self._lock:
            self._pins.clear()


__all__ = [
    "is_address_allowed",
    "HostPinCache",
    "DNSResolutionError",
]
