"""AC12 — SSRF IPv4 + IPv6 + named metadata deny list;
AC12.1 — DNS rebinding defense (IP pin per host session)."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",   # AWS metadata
        "168.63.129.16",     # Azure metadata
        "100.100.100.200",   # Alibaba metadata
        "0.0.0.0",
    ],
)
def test_ac12_ipv4_ssrf_blocks(ip):
    from lavandula.reports.url_guard import is_address_allowed
    assert not is_address_allowed(ip)


@pytest.mark.parametrize(
    "ip",
    [
        "::1",              # loopback
        "fc00::1",          # unique local
        "fd00::1",          # unique local
        "fe80::1",          # link local
        "ff00::1",          # multicast
        "fd00:ec2::254",    # AWS IMDS v6
    ],
)
def test_ac12_ipv6_ssrf_blocks(ip):
    from lavandula.reports.url_guard import is_address_allowed
    assert not is_address_allowed(ip)


def test_ac12_ipv4_mapped_ipv6_normalized():
    """::ffff:10.0.0.1 must be blocked (maps to private IPv4)."""
    from lavandula.reports.url_guard import is_address_allowed
    assert not is_address_allowed("::ffff:10.0.0.1")


def test_ac12_public_ipv4_allowed():
    from lavandula.reports.url_guard import is_address_allowed
    assert is_address_allowed("8.8.8.8")


def test_ac12_public_ipv6_allowed():
    from lavandula.reports.url_guard import is_address_allowed
    assert is_address_allowed("2606:4700:4700::1111")


def test_ac12_1_dns_pin_resolves_once_per_session():
    """AC12.1 — second resolution for the same host returns the pinned IP."""
    from lavandula.reports.url_guard import HostPinCache
    calls = {"n": 0}

    def resolver(host):
        calls["n"] += 1
        # Return different IPs on each call — the cache must NOT be fooled.
        return "8.8.8.8" if calls["n"] == 1 else "127.0.0.1"

    cache = HostPinCache(resolver=resolver)
    assert cache.pin("example.org") == "8.8.8.8"
    assert cache.pin("example.org") == "8.8.8.8"
    assert calls["n"] == 1


def test_ac12_1_dns_rebind_blocked():
    """Second resolution returning a blocked IP does not override the pin."""
    from lavandula.reports.url_guard import HostPinCache
    calls = {"n": 0}

    def resolver(host):
        calls["n"] += 1
        return "8.8.8.8" if calls["n"] == 1 else "127.0.0.1"

    cache = HostPinCache(resolver=resolver)
    first = cache.pin("example.org")
    second = cache.pin("example.org")
    assert first == second == "8.8.8.8"
