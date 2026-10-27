"""Outbound-request guard against SSRF, including DNS rebinding.

Validating a URL's host once and then letting the HTTP client resolve it again leaves a window in
which the name can be re-pointed at an internal address. :func:`guarded_client` closes it: the
client's network backend resolves the host itself, refuses the connection if *any* resolved
address is disallowed, and connects to the vetted IP literal (TLS still verifies the hostname,
since httpcore passes the original host as SNI / ``server_hostname``).

Link-local addresses (cloud metadata services such as 169.254.169.254) are refused even when
private networks are allowed: no legitimate peer of this application lives there.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class BlockedDestination(httpcore.ConnectError):
    """Raised (and surfaced by httpx as ``httpx.ConnectError``) for a disallowed destination."""


def _normalise(ip: str | IPAddress) -> IPAddress:
    addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return addr.ipv4_mapped
        if addr.sixtofour:
            return addr.sixtofour
    return addr


def always_blocked(ip: str | IPAddress) -> bool:
    """Addresses never contacted, whatever the private-network setting says."""
    addr = _normalise(ip)
    return addr.is_link_local or addr.is_multicast or addr.is_unspecified


def ip_blocked(ip: str | IPAddress, allow_private: bool = False) -> bool:
    addr = _normalise(ip)
    if always_blocked(addr):
        return True
    if allow_private:
        return False
    return addr.is_private or addr.is_loopback or addr.is_reserved or not addr.is_global


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that vets every resolved address before connecting."""

    def __init__(
        self, allow_private: bool = False, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self.allow_private = allow_private
        self.inner = inner or httpcore.AnyIOBackend()

    async def _resolve(self, host: str, port: int) -> list[str]:
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(f"Could not resolve {host}") from exc
        return list(dict.fromkeys(str(info[4][0]) for info in infos))

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - signature fixed by httpcore
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        ips = await self._resolve(host, port)
        if not ips or any(ip_blocked(ip, self.allow_private) for ip in ips):
            raise BlockedDestination(f"Destination {host} resolves to a disallowed address")
        return await self.inner.connect_tcp(
            ips[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise BlockedDestination("Unix sockets are not reachable through a guarded client")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def guarded_transport(allow_private: bool = False) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    # httpx exposes no network_backend option; swap in a pool built with the guarded backend.
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        network_backend=GuardedBackend(allow_private),
    )
    return transport


def guarded_client(allow_private: bool = False, **kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that only connects to allowed addresses and never follows redirects.

    Environment proxies are ignored (``trust_env=False``): a proxy would hide the real destination.
    """
    kwargs.setdefault("follow_redirects", False)
    return httpx.AsyncClient(transport=guarded_transport(allow_private), trust_env=False, **kwargs)
