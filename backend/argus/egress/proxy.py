"""Forward proxy that is the browser container's only route to the internet.

It is the SSRF boundary: every destination is resolved once, refused if *any* answer is private,
loopback, link-local, reserved or CGNAT, and the connection is pinned to the first address that was
checked. Only ``CONNECT`` to ports 80/443 and plain ``http://`` GETs on port 80 are served. There is
deliberately no switch to allow private addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from argus.core.logging import get_logger
from argus.core.netguard import ip_blocked

Resolver = Callable[[str], Awaitable[list[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

MAX_HEAD_BYTES = 16 * 1024
HEAD_TIMEOUT_S = 30.0
IDLE_TIMEOUT_S = 120.0
CONNECT_TIMEOUT_S = 15.0
CONNECT_PORTS = {80, 443}

log = get_logger("argus.egress")

_REASONS = {
    200: "Connection Established",
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    502: "Bad Gateway",
}
_HOP_BY_HOP = {"connection", "proxy-connection", "proxy-authorization", "keep-alive", "te", "upgrade", "host"}


async def default_resolver(host: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


class Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _parse_hostport(target: str, default_port: int | None) -> tuple[str, int]:
    if target.startswith("["):
        host, sep, rest = target[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            raise Refused(400, "malformed target")
        port_s = rest[1:]
    else:
        host, sep, port_s = target.rpartition(":")
        if not sep:
            host, port_s = target, ""
        elif ":" in host:
            raise Refused(400, "malformed target")
    if not host:
        raise Refused(400, "malformed target")
    if port_s:
        if not port_s.isdigit() or not 0 < int(port_s) < 65536:
            raise Refused(400, "malformed port")
        port = int(port_s)
    elif default_port is not None:
        port = default_port
    else:
        raise Refused(400, "missing port")
    return host.lower().rstrip("."), port


async def _vet(host: str, resolver: Resolver) -> str:
    """Resolve once and return the checked address to connect to."""
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = list(dict.fromkeys(await resolver(host)))
        except Exception:
            raise Refused(403, "could not resolve host") from None
    if not ips:
        raise Refused(403, "could not resolve host")
    for ip in ips:
        try:
            if ip_blocked(ip, allow_private=False):
                raise Refused(403, "destination resolves to a disallowed address")
        except ValueError:
            raise Refused(403, "destination resolves to a disallowed address") from None
    return ips[0]


async def _respond(writer: asyncio.StreamWriter, status: int, body: str = "") -> None:
    payload = (body + "\n").encode() if body else b""
    reason = _REASONS.get(status, "Error")
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    try:
        writer.write(head.encode() + payload)
        await writer.drain()
    except OSError:
        pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, idle: float) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), idle)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (TimeoutError, OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _tunnel(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
    idle: float,
) -> None:
    """Pipe both ways; the tunnel ends when either side closes or goes idle."""
    tasks = [
        asyncio.ensure_future(_pipe(a_reader, b_writer, idle)),
        asyncio.ensure_future(_pipe(b_reader, a_writer, idle)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for w in (a_writer, b_writer):
            try:
                w.close()
            except OSError:
                pass


async def _read_head(reader: asyncio.StreamReader) -> list[str]:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
        raise Refused(400, "bad request head") from None
    if len(raw) > MAX_HEAD_BYTES:
        raise Refused(400, "request head too large")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise Refused(400, "bad request head") from None
    if any(c in text for c in "\x00") or "\n" in text.replace("\r\n", ""):
        raise Refused(400, "bad request head")
    return text[:-4].split("\r\n")


async def _open(connect: Connector, ip: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        return await asyncio.wait_for(connect(ip, port), CONNECT_TIMEOUT_S)
    except (OSError, TimeoutError):
        raise Refused(502, "upstream connection failed") from None


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    resolver: Resolver,
    connect: Connector,
    idle: float,
) -> None:
    host, port = "-", 0
    try:
        lines = await _read_head(reader)
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            raise Refused(400, "malformed request line")
        method, target, _version = parts
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep or not name or name != name.strip():
                raise Refused(400, "malformed header")
            headers.append((name, value.strip()))

        if method == "CONNECT":
            host, port = _parse_hostport(target, None)
            if port not in CONNECT_PORTS:
                raise Refused(403, "only ports 80 and 443 are allowed")
            ip = await _vet(host, resolver)
            up_reader, up_writer = await _open(connect, ip, port)
            log.info("egress.allow", host=host, port=port, reason="connect")
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _tunnel(reader, writer, up_reader, up_writer, idle)
            return

        if method == "GET" and target.lower().startswith(("http://", "https://")):
            url = urlsplit(target)
            if url.scheme.lower() != "http":
                raise Refused(403, "only plain http is forwarded; use CONNECT for https")
            if url.username or url.password or not url.hostname:
                raise Refused(403, "credentials in URLs are not allowed")
            try:
                url_port = url.port
            except ValueError:
                raise Refused(400, "malformed port") from None
            host, port = url.hostname.lower().rstrip("."), url_port or 80
            if port != 80:
                raise Refused(403, "plain http is only forwarded on port 80")
            if any(k.lower() == "transfer-encoding" for k, _ in headers) or any(
                k.lower() == "content-length" and v not in ("", "0") for k, v in headers
            ):
                raise Refused(400, "request bodies are not allowed")
            ip = await _vet(host, resolver)
            up_reader, up_writer = await _open(connect, ip, port)
            log.info("egress.allow", host=host, port=port, reason="get")
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            host_header = f"[{host}]" if ":" in host else host
            out = [f"GET {path} HTTP/1.1", f"Host: {host_header}"]
            out += [f"{k}: {v}" for k, v in headers if k.lower() not in _HOP_BY_HOP]
            out.append("Connection: close")
            up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode())
            await up_writer.drain()
            await _pipe(up_reader, writer, idle)
            up_writer.close()
            return

        raise Refused(405, "only CONNECT and absolute-form http GET are supported")
    except Refused as refusal:
        if refusal.status in (403, 405):
            log.info("egress.deny", host=host, port=port, reason=refusal.reason)
        await _respond(writer, refusal.status, refusal.reason)
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def serve(
    host: str = "0.0.0.0",  # noqa: S104 - listens inside the egress container's network only
    port: int = 3128,
    *,
    resolver: Resolver = default_resolver,
    connect: Connector = asyncio.open_connection,  # type: ignore[assignment]
    idle_timeout: float = IDLE_TIMEOUT_S,
) -> asyncio.Server:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, resolver, connect, idle_timeout)

    return await asyncio.start_server(handler, host, port, limit=MAX_HEAD_BYTES)


async def _main() -> None:
    server = await serve()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
