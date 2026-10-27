"""Regression tests for the audit follow-ups (SEC-05 residual, SEC-12, SEC-13, SEC-14).

IDs refer to docs/security/audit.md; sandbox isolation (SEC-08) is in test_sandbox_isolation.py.
"""

from __future__ import annotations

import httpx
import pytest

from argus.core.netguard import GuardedBackend, guarded_client
from tests.conftest import register_and_login
from tests.test_security_audit import _bearer, _login


# ------------------------------------------------------------------ peer response size (SEC-12)
async def test_capped_transport_bounds_peer_bodies():
    """SEC-12: a peer cannot make the API buffer an unbounded body (streamed or declared)."""
    from argus.core.netguard import CappedTransport, ResponseTooLarge

    def handler(req):
        if req.url.path == "/declared":
            return httpx.Response(200, headers={"content-length": "5000"}, content=b"x" * 5000)
        if req.url.path == "/streamed":

            async def body():
                for _ in range(10):
                    yield b"y" * 500

            return httpx.Response(200, content=body())
        return httpx.Response(200, content=b"small")

    async with httpx.AsyncClient(transport=CappedTransport(httpx.MockTransport(handler), 1000)) as c:
        assert (await c.get("http://peer/ok")).content == b"small"
        for path in ("/declared", "/streamed"):
            with pytest.raises(ResponseTooLarge):
                await c.get(f"http://peer{path}")
    assert isinstance(guarded_client(max_response_bytes=10)._transport, CappedTransport)


async def test_mcp_client_rejects_oversized_response():
    """SEC-12: the MCP client surfaces an oversized reply as an error instead of buffering it."""
    from argus.core.netguard import CappedTransport
    from argus.protocols.mcp_client import HttpMcpClient, McpClientError

    def handler(req):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"pad": "z" * 5000}})

    client = httpx.AsyncClient(transport=CappedTransport(httpx.MockTransport(handler), 1000))
    mcp = HttpMcpClient("http://peer/mcp", client=client)
    with pytest.raises(McpClientError, match="ResponseTooLarge"):
        await mcp.initialize()
    await mcp.aclose()


def test_outbound_clients_carry_the_configured_cap(settings):
    """SEC-12: connections, remote agents and protocol clients all get a body cap."""
    from argus.core.netguard import DEFAULT_MAX_RESPONSE_BYTES
    from argus.protocols.a2a_client import A2AClient
    from argus.services.connections import outbound_client
    from argus.tools.remote import _peer_client

    settings.protocol_max_response_bytes = 4096
    assert outbound_client(settings, 5)._transport.max_bytes == 4096
    assert _peer_client({"max_response_bytes": 2048})._transport.max_bytes == 2048
    assert A2AClient("http://x").client._transport.max_bytes == DEFAULT_MAX_RESPONSE_BYTES


# ------------------------------------------------------------------ stdio MCP owners (SEC-13)
async def test_stdio_servers_follow_their_owners_admin_role(client, auth, settings):
    """SEC-13: demoting an admin switches off their stdio MCP servers, and they stay off."""
    settings.mcp_allow_stdio = True
    other = await register_and_login(client, "ops@example.com")
    uid = (await client.get("/api/v1/auth/me", headers=other)).json()["id"]
    r = await client.patch(f"/api/v1/admin/users/{uid}", headers=auth, json={"role": "admin"})
    assert r.status_code == 200
    body = {"name": "local", "transport": "stdio", "command": ["true"]}
    r = await client.post("/api/v1/mcp-servers", headers=other, json=body)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    r = await client.patch(f"/api/v1/admin/users/{uid}", headers=auth, json={"role": "member"})
    assert r.status_code == 200
    servers = (await client.get("/api/v1/mcp-servers", headers=other)).json()["servers"]
    assert [s["enabled"] for s in servers] == [False]
    r = await client.patch(f"/api/v1/mcp-servers/{sid}", headers=other, json={"enabled": True})
    assert r.status_code == 403
    assert (await client.post(f"/api/v1/mcp-servers/{sid}/discover", headers=other)).status_code == 403


async def test_deactivation_disables_stdio_servers(client, auth, settings):
    """SEC-13: deactivating an admin does the same."""
    settings.mcp_allow_stdio = True
    other = await register_and_login(client, "ops2@example.com")
    uid = (await client.get("/api/v1/auth/me", headers=other)).json()["id"]
    await client.patch(f"/api/v1/admin/users/{uid}", headers=auth, json={"role": "admin"})
    body = {"name": "local", "transport": "stdio", "command": ["true"]}
    assert (await client.post("/api/v1/mcp-servers", headers=other, json=body)).status_code == 201
    r = await client.patch(f"/api/v1/admin/users/{uid}", headers=auth, json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    from sqlalchemy import select

    from argus.db.models import McpServer

    app = client._transport.app  # type: ignore[attr-defined]
    async with app.state.db.sessionmaker() as s:
        rows = list((await s.execute(select(McpServer))).scalars())
    assert [r.enabled for r in rows] == [False]


async def test_attach_connections_skips_stdio_of_non_admin_owner(app, auth, settings, monkeypatch):
    """SEC-13: even if a row is still enabled, a run never spawns a non-admin's stdio server."""
    from sqlalchemy import select

    import argus.services.connections as conns
    from argus.db.models import McpServer, User
    from argus.tools.base import ToolRegistry

    settings.mcp_allow_stdio = True
    maker = app.state.db.sessionmaker
    async with maker() as s:
        user = (await s.execute(select(User))).scalar_one()
        user.role = "member"
        s.add(McpServer(owner_id=user.id, name="local", transport="stdio", command=["true"], risk=[]))
        await s.commit()
        owner_id = user.id

    def boom(*a, **k):
        raise AssertionError("stdio server must not be started")

    monkeypatch.setattr(conns, "build_mcp_client", boom)
    events = []

    async def emit(type_, data):
        events.append((type_, data))

    await conns.attach_connections(ToolRegistry(), maker, owner_id, settings, emit)
    assert [e[1]["code"] for e in events] == ["mcp_stdio_not_permitted"]


# ------------------------------------------------------------------ provider endpoints (SEC-14)
async def test_user_provider_clients_recheck_addresses_at_connect():
    """SEC-14: user-configured provider endpoints go through the connect-time guard."""
    from argus.providers.base import ProviderError
    from argus.providers.catalog import build_provider

    p = build_provider("ollama", base_url="http://127.0.0.1:9/v1", guard_private=False)
    with pytest.raises(ProviderError) as exc:
        await p.list_models()
    assert "disallowed" in str(exc.value.__cause__)
    # Private model servers work when the operator allows them; link-local (metadata) never does.
    p = build_provider("ollama", base_url="http://169.254.169.254/v1", guard_private=True)
    with pytest.raises(ProviderError) as exc:
        await p.list_models()
    assert "disallowed" in str(exc.value.__cause__)
    # Operator-configured system providers (guard_private=None) keep a plain client.
    plain = build_provider("ollama", base_url="http://x/v1").client._transport
    assert not isinstance(getattr(plain, "_pool", None) and plain._pool._network_backend, GuardedBackend)


async def test_anthropic_provider_uses_guarded_httpx2_client():
    """SEC-14: the Anthropic SDK (which uses httpx2) gets the same guard."""
    import httpx2

    from argus.core.netguard import guarded_httpx2_client
    from argus.providers.catalog import build_provider

    async with guarded_httpx2_client(False) as c:
        with pytest.raises(httpx2.ConnectError, match="disallowed"):
            await c.get("http://127.0.0.1:9/")
    p = build_provider("anthropic", api_key="k", base_url="http://127.0.0.1:9", guard_private=False)
    backend = p.client._client._transport._pool._network_backend
    assert isinstance(backend, GuardedBackend) and backend.allow_private is False


async def test_router_guards_user_providers_with_the_setting(client, auth, app, settings):
    """SEC-14: the LLM router builds user providers with provider_allow_private_urls."""
    from sqlalchemy import select

    from argus.db.models import ProviderConfig

    body = {"name": "lan", "kind": "ollama", "base_url": "http://10.0.0.5:11434/v1"}
    settings.provider_allow_private_urls = False
    assert (await client.post("/api/v1/providers", headers=auth, json=body)).status_code == 400
    settings.provider_allow_private_urls = True
    assert (await client.post("/api/v1/providers", headers=auth, json=body)).status_code == 201
    async with app.state.db.sessionmaker() as s:
        cfg = (await s.execute(select(ProviderConfig))).scalar_one()
    backend = app.state.llm.provider_for_config(cfg).client._transport._pool._network_backend
    assert isinstance(backend, GuardedBackend) and backend.allow_private is True


# ------------------------------------------------------------------ revoked sessions everywhere
async def test_signed_out_token_rejected_on_protocols_and_stream(client, auth):
    """SEC-05 residual: /mcp, /a2a, /acp and the SSE stream also honour session revocation."""
    run = (
        await client.post("/api/v1/runs", headers=auth, json={"topic": "t", "mode": "chat", "size": 2})
    ).json()
    t = await _login(client)
    await client.post("/api/v1/auth/logout", json={"refresh_token": t["refresh_token"]})
    h = _bearer(t)
    r = await client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    params = {"id": run["id"]}
    r = await client.post(
        "/a2a", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": params}
    )
    assert r.status_code == 401
    assert (await client.get("/acp/agents", headers=h)).status_code == 401
    assert (await client.get(f"/api/v1/runs/{run['id']}/events/stream", headers=h)).status_code == 401
