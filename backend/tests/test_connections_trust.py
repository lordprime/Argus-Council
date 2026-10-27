"""MCP trust levels, remote-agent approval, discovery ledgering and skill usage."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

import argus.api.v1.connections as conn_api
import argus.services.connections as conns
from argus.db.models import McpServer, RemoteAgent
from argus.protocols.a2a_client import A2AClient
from argus.protocols.acp_client import AcpClient
from argus.protocols.mcp_client import McpClient
from argus.services.connections import (
    APPROVAL_RISK,
    GatedRemoteAgentTool,
    attach_connections,
    mcp_settings,
    mcp_tool_risk,
)
from argus.tools.base import ToolRegistry


class FakeMcp(McpClient):
    instances: list[FakeMcp] = []

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        FakeMcp.instances.append(self)

    async def _request(self, method, params):
        if self.fail:
            raise ConnectionError("boom")
        if method == "initialize":
            return {"serverInfo": {"name": "fake"}}
        if method == "tools/list":
            return {"tools": [{"name": "read"}, {"name": "write"}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    async def _notify(self, method, params=None):
        return None


async def _ledger(client, auth, **params):
    return (await client.get("/api/v1/ledger", headers=auth, params=params)).json()["entries"]


async def _row(app, model, obj_id):
    async with app.state.db.sessionmaker() as s:
        return await s.get(model, uuid.UUID(obj_id))


async def test_mcp_trust_api(client, auth, app):
    base = {"url": "https://mcp.example.com/mcp"}
    r = await client.post("/api/v1/mcp-servers", headers=auth, json={"name": "a", **base})
    a = r.json()
    assert r.status_code == 201 and a["trust"] == "ask" and a["require_approval"] == []
    assert a["risk"] == ["network"]
    r = await client.post(
        "/api/v1/mcp-servers",
        headers=auth,
        json={"name": "b", "trust": "trusted", "require_approval": ["write"], **base},
    )
    assert (r.json()["trust"], r.json()["require_approval"]) == ("trusted", ["write"])
    bad = await client.post("/api/v1/mcp-servers", headers=auth, json={"name": "c", "trust": "maybe", **base})
    assert bad.status_code == 422

    r = await client.patch(f"/api/v1/mcp-servers/{a['id']}", headers=auth, json={"trust": "blocked"})
    assert r.json()["trust"] == "blocked" and r.json()["risk"] == ["network"]
    r = await client.patch(
        f"/api/v1/mcp-servers/{a['id']}", headers=auth, json={"risk": [], "require_approval": ["x", "x"]}
    )
    assert (r.json()["trust"], r.json()["risk"], r.json()["require_approval"]) == ("blocked", [], ["x"])
    r = await client.patch(f"/api/v1/mcp-servers/{a['id']}", headers=auth, json={"enabled": False})
    assert r.json()["trust"] == "blocked" and r.json()["require_approval"] == ["x"]
    listed = (await client.get("/api/v1/mcp-servers", headers=auth)).json()["servers"]
    assert {s["name"]: s["trust"] for s in listed} == {"a": "blocked", "b": "trusted"}


def test_trust_risk_mapping():
    legacy = McpServer(name="l", risk=["network"])
    assert mcp_settings(legacy) == ("trusted", [], ["network"])  # pre-existing rows keep old behaviour
    assert mcp_tool_risk(legacy, "any") == ["network"]
    ask = McpServer(name="a", risk=["network", "trust:ask"])
    assert mcp_tool_risk(ask, "any") == ["network", APPROVAL_RISK]
    trusted = McpServer(name="t", risk=["network", "trust:trusted", "approve:write"])
    assert mcp_tool_risk(trusted, "read") == ["network"]
    assert mcp_tool_risk(trusted, "write") == ["network", APPROVAL_RISK]


async def test_attach_honours_trust_and_ledgers_discovery(client, auth, app, monkeypatch):
    FakeMcp.instances.clear()
    monkeypatch.setattr(conns, "build_mcp_client", lambda server, s, b: FakeMcp())
    base = {"url": "https://mcp.example.com/mcp"}
    for name, trust in (("trust", "trusted"), ("asky", "ask"), ("nope", "blocked")):
        body = {"name": name, "trust": trust, **base}
        if name == "trust":
            body["require_approval"] = ["write"]
        assert (await client.post("/api/v1/mcp-servers", headers=auth, json=body)).status_code == 201
    async with app.state.db.sessionmaker() as s:
        owner_id = (await s.execute(select(McpServer.owner_id).limit(1))).scalar_one()

    class Sink:
        run_id = uuid.uuid4()
        events: list = []

        async def emit(self, type, data=None, **kw):
            self.events.append((type, data))

    sink = Sink()
    registry = ToolRegistry()
    closers = await attach_connections(
        registry, app.state.db.sessionmaker, owner_id, app.state.settings, sink.emit
    )
    assert len(FakeMcp.instances) == 2  # the blocked server is never contacted
    tools = {n: registry.get(n) for n in ("mcp__trust__read", "mcp__trust__write", "mcp__asky__read")}
    assert all(tools.values()) and registry.get("mcp__nope__read") is None
    assert APPROVAL_RISK not in tools["mcp__trust__read"].risk
    assert APPROVAL_RISK in tools["mcp__trust__write"].risk and APPROVAL_RISK in tools["mcp__asky__read"].risk
    for c in closers:
        await c()

    entries = await _ledger(client, auth, protocol="mcp")
    run_entries = [e for e in entries if e["run_id"] == str(sink.run_id)]
    assert sorted(e["method"] for e in run_entries) == [
        "initialize",
        "initialize",
        "tools/list",
        "tools/list",
    ]
    assert all(e["direction"] == "outbound" and e["status"] == "ok" for e in run_entries)
    assert {e["detail"]["server"] for e in run_entries} == {"trust", "asky"}

    # discover is ledgered too, including failures
    sid = (await client.get("/api/v1/mcp-servers", headers=auth)).json()["servers"][0]["id"]
    assert (await client.post(f"/api/v1/mcp-servers/{sid}/discover", headers=auth)).status_code == 200
    monkeypatch.setattr(conns, "build_mcp_client", lambda server, s, b: FakeMcp(fail=True))
    assert (await client.post(f"/api/v1/mcp-servers/{sid}/discover", headers=auth)).status_code == 502
    disc = [
        e for e in await _ledger(client, auth, protocol="mcp") if e["detail"].get("purpose") == "discover"
    ]
    assert [(e["method"], e["status"]) for e in reversed(disc)] == [
        ("initialize", "ok"),
        ("tools/list", "ok"),
        ("initialize", "error"),
    ]
    assert (await client.get("/api/v1/ledger/verify", headers=auth)).json()["ok"] is True


async def test_remote_agent_approval_and_acp_rename(client, auth, app, monkeypatch):
    r = await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={"name": "acp1", "protocol": "acp", "url": "https://acp.example.com", "agent_name": "one"},
    )
    agent = r.json()
    assert r.status_code == 201 and agent["requires_approval"] is True and agent["card"] == {}
    r = await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={"name": "a2a1", "protocol": "a2a", "url": "http://test/a2a", "requires_approval": False},
    )
    a2a = r.json()
    assert a2a["requires_approval"] is False

    # ACP refresh is ledgered (failure and success)
    listing = {"agents": [{"name": "two", "description": "Second agent"}]}

    class FakeAcp(AcpClient):
        def __init__(self, base_url, token=None, timeout=60.0, client=None):
            transport = httpx.MockTransport(lambda req: httpx.Response(200, json=listing))
            super().__init__(base_url, token, timeout, client=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(conn_api, "AcpClient", FakeAcp)
    r = await client.post(f"/api/v1/remote-agents/{agent['id']}/refresh", headers=auth)
    assert r.status_code == 502 and "one" in r.text
    r = await client.patch(f"/api/v1/remote-agents/{agent['id']}", headers=auth, json={"agent_name": "two"})
    assert r.json()["agent_name"] == "two"
    r = await client.post(f"/api/v1/remote-agents/{agent['id']}/refresh", headers=auth)
    assert (
        r.status_code == 200 and r.json()["card"]["name"] == "two" and r.json()["requires_approval"] is True
    )
    assert r.json()["description"] == "Second agent"
    acp = await _ledger(client, auth, protocol="acp")
    assert [(e["method"], e["direction"], e["status"]) for e in acp] == [
        ("agents:list", "outbound", "ok")
    ] * 2

    r = await client.patch(
        f"/api/v1/remote-agents/{agent['id']}", headers=auth, json={"requires_approval": False}
    )
    assert r.json()["requires_approval"] is False and r.json()["card"]["name"] == "two"
    r = await client.patch(f"/api/v1/remote-agents/{agent['id']}", headers=auth, json={"agent_name": "three"})
    assert r.json()["card"] == {} and r.json()["requires_approval"] is False
    r = await client.patch(f"/api/v1/remote-agents/{a2a['id']}", headers=auth, json={"agent_name": "x"})
    assert r.status_code == 400

    # A2A card refresh is ledgered and keeps the approval setting
    class LoopA2A(A2AClient):
        def __init__(self, url, token=None, timeout=60.0, client=None):
            super().__init__(
                url,
                token,
                timeout,
                client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
            )

    monkeypatch.setattr(conn_api, "A2AClient", LoopA2A)
    r = await client.post(f"/api/v1/remote-agents/{a2a['id']}/refresh", headers=auth)
    assert r.status_code == 200 and r.json()["card"]["name"] == "Argus Council"
    assert r.json()["requires_approval"] is False
    a2a_entries = await _ledger(client, auth, protocol="a2a")
    assert [(e["method"], e["direction"], e["status"]) for e in a2a_entries] == [
        ("agent-card", "outbound", "ok")
    ]
    row = await _row(app, RemoteAgent, a2a["id"])
    assert row.card["x-argus"] == {"requires_approval": False}


def test_gated_remote_agent_risk():
    tool = GatedRemoteAgentTool(
        {
            "strict": {"protocol": "a2a", "url": "u", "requires_approval": True},
            "free": {"protocol": "a2a", "url": "u", "requires_approval": False},
            "legacy": {"protocol": "a2a", "url": "u"},
        }
    )
    risk = {
        n: tool.risk_for(tool.Args(agent=n, message="m")) for n in ("strict", "free", "legacy", "unknown")
    }
    assert risk["strict"] == risk["legacy"] == risk["unknown"] == frozenset({"remote_agent"})
    assert risk["free"] == frozenset()


async def test_skill_used_by_councils(client, auth):
    name = (await client.get("/api/v1/skills", headers=auth)).json()["skills"][0]["name"]

    async def council(cname, skills=None):
        body = {"name": cname, "size": 2, "mode": "chat"}
        if skills is not None:
            body["guardrails"] = {"skills": skills}
        r = await client.post("/api/v1/councils", headers=auth, json=body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    default = await council("default")
    named = await council("named", {"allow": [name]})
    await council("other", {"allow": ["zzz-none"]})
    await council("denied", {"allow": ["*"], "deny": [name]})
    r = await client.get(f"/api/v1/skills/{name}", headers=auth)
    assert r.json()["used_by_councils"] == [
        {"id": default, "name": "default"},
        {"id": named, "name": "named"},
    ]
    usage = (await client.get(f"/api/v1/skills/{name}/usage", headers=auth)).json()
    assert usage == {"name": name, "used_by_councils": r.json()["used_by_councils"]}
    assert (await client.get("/api/v1/skills/no-such-skill/usage", headers=auth)).status_code == 404
