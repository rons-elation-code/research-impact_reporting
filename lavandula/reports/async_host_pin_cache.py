"""Async DNS pin cache implementing aiohttp's resolver interface (Spec 0021, AC10-AC14).

Resolves hostname -> IP via getaddrinfo in an executor (non-blocking),
checks is_address_allowed, caches for the session lifetime. Negative
results are also cached to prevent CPU-amplification via repeated
resolution of private-IP hosts.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.client_reqrep import ConnectionKey

from .url_guard import DNSResolutionError, is_address_allowed


class AsyncHostPinCache(AbstractResolver):
    """DNS pin cache for aiohttp — single event loop only."""

    def __init__(self) -> None:
        self._positive: dict[str, tuple[str, int]] = {}  # host -> (ip, family)
        self._negative: set[str] = set()
        self._lock = asyncio.Lock()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        host_key = host.lower()

        async with self._lock:
            if host_key in self._negative:
                raise aiohttp.ClientConnectorError(
                    connection_key=ConnectionKey(
                        host=host, port=port, is_ssl=False,
                        ssl=None, proxy=None, proxy_auth=None,
                        proxy_headers_hash=None,
                    ),
                    os_error=OSError(f"DNS pin rejected (cached): {host_key}"),
                )

            cached = self._positive.get(host_key)
            if cached is not None:
                ip, fam = cached
                return [
                    {
                        "hostname": host,
                        "host": ip,
                        "port": port,
                        "family": fam,
                        "proto": 0,
                        "flags": socket.AI_NUMERICHOST,
                    }
                ]

        loop = asyncio.get_running_loop()
        ip, fam = await self._do_resolve(loop, host, port)

        if not is_address_allowed(ip):
            async with self._lock:
                self._negative.add(host_key)
            raise aiohttp.ClientConnectorError(
                connection_key=ConnectionKey(
                    host=host, port=port, is_ssl=False,
                    ssl=None, proxy=None, proxy_auth=None,
                    proxy_headers_hash=None,
                ),
                os_error=OSError(
                    f"DNS pin rejected: {host_key} -> {ip} (disallowed)"
                ),
            )

        async with self._lock:
            self._positive[host_key] = (ip, fam)

        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": fam,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    @staticmethod
    async def _do_resolve(
        loop: asyncio.AbstractEventLoop, host: str, port: int
    ) -> tuple[str, int]:
        """Run getaddrinfo in executor; prefer IPv4."""
        infos = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(
                host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            ),
        )
        if not infos:
            raise OSError(f"no address for {host}")

        ipv4 = [i for i in infos if i[0] == socket.AF_INET]
        if ipv4:
            return ipv4[0][4][0], socket.AF_INET

        ipv6 = [i for i in infos if i[0] == socket.AF_INET6]
        if ipv6:
            return ipv6[0][4][0], socket.AF_INET6

        return infos[0][4][0], infos[0][0]

    async def close(self) -> None:
        pass


__all__ = ["AsyncHostPinCache"]
