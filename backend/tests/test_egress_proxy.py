import asyncio

import pytest

from argus.egress import proxy as egress

PUBLIC = "93.184.216.34"


class Harness:
    def __init__(self, ips, echo_port):
        self.ips = ips
        self.echo_port = echo_port
        self.resolved: list[str] = []
        self.connected: list[tuple[str, int]] = []

    async def resolver(self, host):
        self.resolved.append(host)
        return list(self.ips)

    async def connect(self, host, port):
        self.connected.append((host, port))
        return await asyncio.open_connection("127.0.0.1", self.echo_port)


async def _echo(reader, writer):
    while data := await reader.read(1024):
        if data.startswith(b"GET "):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
            await writer.drain()
            break
        writer.write(data)
        await writer.drain()
    writer.close()


@pytest.fixture
async def echo():
    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


async def start(ips, echo_port, **kwargs):
    h = Harness(ips, echo_port)
    server = await egress.serve("127.0.0.1", 0, resolver=h.resolver, connect=h.connect, **kwargs)
    return h, server, server.sockets[0].getsockname()[1]


async def exchange(port, payload: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(payload)
    await w.drain()
    data = await asyncio.wait_for(r.read(4096), 5)
    w.close()
    return data


def status(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1])


async def connect_status(port, target):
    return status(await exchange(port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()))


async def test_connect_to_public_host_tunnels_bytes(echo):
    _h, server, port = await start([PUBLIC], echo)
    async with server:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT ok.test:443 HTTP/1.1\r\nHost: ok.test:443\r\n\r\n")
        await w.drain()
        head = await r.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        w.write(b"ping")
        await w.drain()
        assert await asyncio.wait_for(r.read(4), 5) == b"ping"
        w.close()


@pytest.mark.parametrize(
    "ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "100.64.0.1", "192.168.1.1"]
)
async def test_connect_refuses_private_loopback_linklocal(echo, ip):
    h, server, port = await start([ip], echo)
    async with server:
        assert await connect_status(port, "x.test:443") == 403
        assert h.connected == []


async def test_connect_refuses_other_ports(echo):
    h, server, port = await start([PUBLIC], echo)
    async with server:
        assert await connect_status(port, "x.test:22") == 403
        assert await connect_status(port, "x.test:8443") == 403
        assert h.connected == []


async def test_ip_literal_targets_are_checked(echo):
    h, server, port = await start([PUBLIC], echo)
    async with server:
        assert await connect_status(port, "127.0.0.1:443") == 403
        assert await connect_status(port, "[::1]:443") == 403
        assert await connect_status(port, "[fe80::1]:443") == 403
        assert h.connected == []


async def test_dns_answer_mixing_public_and_private_is_refused(echo):
    h, server, port = await start([PUBLIC, "10.0.0.5"], echo)
    async with server:
        assert await connect_status(port, "x.test:443") == 403
        assert h.connected == []


async def test_resolves_once_and_connects_to_the_checked_address(echo):
    h, server, port = await start([PUBLIC, "93.184.216.35"], echo)
    async with server:
        assert await connect_status(port, "x.test:443") == 200
        assert h.resolved == ["x.test"]
        assert h.connected == [(PUBLIC, 443)]


async def test_plain_http_get_is_forwarded_and_post_is_refused(echo):
    h, server, port = await start([PUBLIC], echo)
    async with server:
        raw = await exchange(
            port,
            b"GET http://ok.test/a?b=1 HTTP/1.1\r\nHost: ok.test\r\nProxy-Connection: keep-alive\r\n\r\n",
        )
        assert status(raw) == 200 and raw.endswith(b"hi")
        assert h.connected == [(PUBLIC, 80)]
        post = await exchange(
            port, b"POST http://ok.test/ HTTP/1.1\r\nHost: ok.test\r\nContent-Length: 0\r\n\r\n"
        )
        assert status(post) == 405


async def test_plain_http_get_refuses_private_other_ports_and_https(echo):
    _h, server, port = await start(["10.0.0.5"], echo)
    async with server:
        assert status(await exchange(port, b"GET http://x.test/ HTTP/1.1\r\nHost: x.test\r\n\r\n")) == 403
    h2, server2, port2 = await start([PUBLIC], echo)
    async with server2:
        assert (
            status(await exchange(port2, b"GET http://x.test:8080/ HTTP/1.1\r\nHost: x.test\r\n\r\n")) == 403
        )
        assert status(await exchange(port2, b"GET https://x.test/ HTTP/1.1\r\nHost: x.test\r\n\r\n")) == 403
        assert status(await exchange(port2, b"GET /relative HTTP/1.1\r\nHost: x.test\r\n\r\n")) == 405
        assert h2.connected == []


async def test_garbage_and_oversized_heads_get_400(echo):
    h, server, port = await start([PUBLIC], echo)
    async with server:
        assert status(await exchange(port, b"\x00\x01garbage\r\n\r\n")) == 400
        big = b"GET http://x.test/ HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n"
        assert status(await exchange(port, big)) == 400
        assert h.connected == []


async def test_idle_tunnel_is_closed(echo):
    _h, server, port = await start([PUBLIC], echo, idle_timeout=0.2)
    async with server:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT ok.test:443 HTTP/1.1\r\n\r\n")
        await r.readuntil(b"\r\n\r\n")
        assert await asyncio.wait_for(r.read(10), 3) == b""
        w.close()
