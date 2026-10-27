"""Regression tests for the findings in docs/security/audit.md (IDs in each test's docstring)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from argus.core.netguard import BlockedDestination, GuardedBackend, guarded_client, ip_blocked
from argus.runtime.guardrails import Guardrails
from argus.services.llm import InvalidProviderURL, validate_base_url
from tests.conftest import register_and_login
from tests.test_exports_and_tools import _Ctx

PW = "correct horse battery staple"


# ------------------------------------------------------------------ SSRF (SEC-02, SEC-03)
@pytest.mark.parametrize(
    "ip,allow_private,blocked",
    [
        ("93.184.216.34", False, False),
        ("10.0.0.5", False, True),
        ("10.0.0.5", True, False),
        ("127.0.0.1", False, True),
        ("100.64.0.1", False, True),  # carrier-grade NAT: not global
        ("169.254.169.254", True, True),  # cloud metadata: refused even when private is allowed
        ("::ffff:169.254.169.254", True, True),
        ("fe80::1", True, True),
        ("0.0.0.0", True, True),  # noqa: S104 - an address under test, not a bind
        ("::1", False, True),
    ],
)
def test_ip_blocked_matrix(ip, allow_private, blocked):
    """SEC-02/SEC-03: one address policy for every outbound path."""
    assert ip_blocked(ip, allow_private) is blocked


async def test_guarded_client_refuses_loopback_and_metadata():
    """SEC-03: the guard runs at connect time, after DNS, so it cannot be bypassed by a URL check."""
    async with guarded_client(False) as c:
        with pytest.raises(httpx.ConnectError, match="disallowed"):
            await c.get("http://127.0.0.1:9/")
    async with guarded_client(True) as c:
        with pytest.raises(httpx.ConnectError, match="disallowed"):
            await c.get("http://169.254.169.254/latest/meta-data/")


async def test_guarded_backend_blocks_dns_rebinding():
    """SEC-03: a name that resolves to any private address is refused before connecting."""

    class Rebinding(GuardedBackend):
        async def _resolve(self, host, port):
            return ["93.184.216.34", "10.0.0.1"]

    with pytest.raises(BlockedDestination):
        await Rebinding(False).connect_tcp("rebind.example", 80)


def test_validate_base_url_policy():
    """SEC-03: link-local always refused; private refused unless allowed; DNS optional for guarded peers."""
    with pytest.raises(InvalidProviderURL, match="link-local"):
        validate_base_url("http://169.254.169.254/v1", True)
    with pytest.raises(InvalidProviderURL, match="private"):
        validate_base_url("http://10.0.0.1/v1", False)
    assert validate_base_url("http://10.0.0.1/v1/", True) == "http://10.0.0.1/v1"
    assert validate_base_url("https://hooks.invalid/x", False, require_dns=False) == "https://hooks.invalid/x"
    with pytest.raises(InvalidProviderURL, match="resolve"):
        validate_base_url("https://hooks.invalid/x", False)


async def test_connections_refuse_internal_urls(client, auth):
    """SEC-03: webhooks, MCP servers, remote agents and providers cannot target internal hosts."""
    cases = [
        ("/api/v1/webhooks", {"url": "https://127.0.0.1:9/hook", "events": ["*"]}),
        ("/api/v1/mcp-servers", {"name": "m", "url": "http://10.0.0.1/mcp"}),
        (
            "/api/v1/remote-agents",
            {"name": "a", "protocol": "a2a", "url": "http://[::ffff:169.254.169.254]/a2a"},
        ),
        ("/api/v1/providers", {"name": "p", "kind": "openai", "base_url": "http://169.254.169.254/v1"}),
    ]
    for path, body in cases:
        r = await client.post(path, headers=auth, json=body)
        assert r.status_code == 400, (path, r.text)
        assert r.json()["error"]["code"] == "invalid_provider_url", r.text


async def test_webhook_dispatcher_uses_guarded_client(settings):
    """SEC-03: deliveries re-check the destination on every connect (DNS rebinding)."""
    from argus.services.webhooks import WebhookDispatcher

    d = WebhookDispatcher(settings, sessionmaker=None)
    async with d.client_factory() as c:
        with pytest.raises(httpx.ConnectError, match="disallowed"):
            await c.post("http://127.0.0.1:9/hook", content=b"{}")


async def test_web_fetch_guardrail_cannot_open_private_networks():
    """SEC-02: a run's guardrails can only tighten the server's private-network ceiling."""
    from argus.tools.web import WebFetch

    async def private(host):
        return ["10.0.0.5"]

    def handler(req):
        return httpx.Response(200, text="internal secret")

    rails = Guardrails.resolve({"network": {"block_private_ips": False}})
    tool = WebFetch(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=private
    )
    res = await tool.invoke({"url": "http://internal.corp/"}, _Ctx(rails))
    assert res.is_error and "private" in res.content and "secret" not in res.content
    # With the operator's opt-in, the run's own setting decides; metadata stays blocked regardless.
    opened = WebFetch(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=private,
        allow_private=True,
    )
    assert not (await opened.invoke({"url": "http://internal.corp/"}, _Ctx(rails))).is_error

    async def metadata(host):
        return ["169.254.169.254"]

    opened.resolver = metadata
    assert (await opened.invoke({"url": "http://metadata.internal/"}, _Ctx(rails))).is_error


# ------------------------------------------------------------------ brute force (SEC-01)
async def test_login_throttled_per_account_across_ips(app, auth, monkeypatch):
    """SEC-01: spreading attempts over many client IPs does not escape the per-account limit."""
    import argus.cache.store as store

    monkeypatch.setattr(store.time, "time", lambda: 1_700_000_000.0)
    codes = []
    for i in range(12):
        transport = ASGITransport(app=app, client=(f"203.0.113.{i + 1}", 1234))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "bad"})
            codes.append(r.status_code)
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


def test_nginx_overwrites_forwarded_for():
    """SEC-01: nginx must not append to a client-supplied X-Forwarded-For (uvicorn trusts the left-most)."""
    conf = Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"
    if not conf.exists():
        pytest.skip("frontend not checked out")
    text = conf.read_text()
    assert "$proxy_add_x_forwarded_for" not in text
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in text


# ------------------------------------------------------------------ sessions (SEC-05, SEC-06)
async def _login(client, email="admin@example.com") -> dict:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_logout_ends_session_server_side(client, auth):
    """SEC-05: logout revokes the refresh token and the session's access tokens."""
    t = await _login(client)
    assert (await client.get("/api/v1/auth/me", headers=_bearer(t))).status_code == 200
    r = await client.post("/api/v1/auth/logout", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=_bearer(t))).status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 401
    # Other sessions are untouched.
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200
    # Logging out with a garbage token is a harmless no-op.
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": "x"})).status_code == 204


async def test_refresh_reuse_revokes_whole_session(client, auth):
    """SEC-05: replaying a used refresh token ends the session for both holders."""
    t1 = await _login(client)
    t2 = (await client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})).json()
    assert (await client.get("/api/v1/auth/me", headers=_bearer(t2))).status_code == 200
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=_bearer(t2))).status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]})
    assert r.status_code == 401


async def test_concurrent_refresh_only_one_wins(client, auth):
    """SEC-06: check-and-mark is atomic, so a raced refresh token yields one new pair, not two."""
    t = await _login(client)
    rs = await asyncio.gather(
        *(client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}) for _ in range(4))
    )
    assert sorted(r.status_code for r in rs) == [200, 401, 401, 401]


async def test_password_change_signs_out_other_sessions(client, auth, monkeypatch):
    """SEC-05: changing the password ends every other session; the current one keeps working."""
    import argus.services.sessions as sessions

    other = await _login(client)
    current = await _login(client)
    # Tokens carry whole-second iat: make the cutoff land strictly after both logins.
    later = time.time() + 5
    monkeypatch.setattr(sessions, "time", SimpleNamespace(time=lambda: later))
    r = await client.patch(
        "/api/v1/auth/me",
        headers=_bearer(current),
        json={"current_password": PW, "new_password": "another long passphrase"},
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/auth/me", headers=_bearer(current))).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=_bearer(other))).status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": other["refresh_token"]})
    assert r.status_code == 401
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": current["refresh_token"]})
    assert r.status_code == 200


# ------------------------------------------------------------------ admin via API key (SEC-04)
async def test_scoped_admin_key_cannot_change_users(client, auth):
    """SEC-04: an admin's read-only (or narrowly scoped) key cannot change roles or run retention."""
    victim = await register_and_login(client, "victim@example.com")
    uid = (await client.get("/api/v1/auth/me", headers=victim)).json()["id"]

    async def key(scopes):
        r = await client.post("/api/v1/auth/api-keys", headers=auth, json={"name": "k", "scopes": scopes})
        return {"Authorization": f"Bearer {r.json()['key']}"}

    for scopes in (["read"], ["runs:write"]):
        h = await key(scopes)
        assert (await client.get("/api/v1/admin/users", headers=h)).status_code == 200
        r = await client.patch(f"/api/v1/admin/users/{uid}", headers=h, json={"role": "admin"})
        assert r.status_code == 403, r.text
        r = await client.post("/api/v1/admin/retention/run", headers=h, json={"dry_run": True})
        assert r.status_code == 403, r.text
    full = await key(["*"])
    r = await client.patch(f"/api/v1/admin/users/{uid}", headers=full, json={"role": "viewer"})
    assert r.status_code == 200 and r.json()["role"] == "viewer"


# ------------------------------------------------------------------ prior fixes stay fixed
async def test_prior_fixes_hold(client, auth):
    """SEC-P1/SEC-P2: empty-scope keys are read-only; read-only keys cannot write over protocols."""
    r = await client.post("/api/v1/auth/api-keys", headers=auth, json={"name": "e", "scopes": []})
    h = {"Authorization": f"Bearer {r.json()['key']}"}
    r = await client.post("/api/v1/runs", headers=h, json={"topic": "t", "mode": "chat", "size": 2})
    assert r.status_code == 403
    r = await client.post(
        "/mcp",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "start_run", "arguments": {"topic": "x"}},
        },
    )
    assert r.json()["result"]["isError"] is True


# ------------------------------------------------------------------ A2A bounds (SEC-11)
@pytest.mark.parametrize(
    "raw,expected",
    [(None, 600.0), ("abc", 600.0), (1e9, 600.0), (-5, 0.0), (float("nan"), 600.0), (30, 30.0)],
)
def test_a2a_blocking_wait_is_bounded(raw, expected):
    """SEC-11: a client-chosen timeout cannot hold a worker (and its DB session) indefinitely."""
    from argus.protocols.a2a import _wait_seconds

    assert _wait_seconds(raw) == expected


# ------------------------------------------------------------------ sandbox transport (SEC-07)
async def test_remote_sandbox_over_unix_socket(monkeypatch):
    """SEC-07: the API reaches a network-less sandbox over a Unix socket, token still required."""
    import uvicorn

    import argus.sandbox.server as server
    from argus.sandbox.client import RemoteSandbox

    async def fake_execute(files, command, **kw):
        return {"exit_code": 0, "stdout": f"ran {command}", "stderr": "", "changed": {}, "deleted": []}

    monkeypatch.setattr(server, "_TOKEN", "sandbox-token")
    monkeypatch.setattr(server, "execute", fake_execute)
    tmp = tempfile.mkdtemp()  # short path: Unix socket paths are limited to ~100 bytes
    sock = f"{tmp}/sandbox.sock"
    srv = uvicorn.Server(uvicorn.Config(server.app, uds=sock, log_level="warning", lifespan="off"))
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(200):
            if srv.started:
                break
            await asyncio.sleep(0.01)
        res = await RemoteSandbox(f"unix:{sock}", "sandbox-token").run({}, "echo hi", 5)
        assert res["stdout"] == "ran echo hi"
        with pytest.raises(RuntimeError, match="401"):
            await RemoteSandbox(f"unix:{sock}", "wrong").run({}, "echo hi", 5)
    finally:
        srv.should_exit = True
        await task
        shutil.rmtree(tmp, ignore_errors=True)
