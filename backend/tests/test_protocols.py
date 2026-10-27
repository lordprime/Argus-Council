import json
import sys
import textwrap

import httpx
import pytest

from argus.protocols.a2a_client import A2AClient, task_text
from argus.protocols.acp_client import AcpClient, run_text
from argus.protocols.mcp_client import HttpMcpClient, McpClientError, StdioMcpClient, render_result, tool_name
from argus.providers.mock import MockProvider, council_responder
from tests.test_runs import events_of, wait_status


@pytest.fixture
async def api_key(client, auth):
    return (await client.post("/api/v1/auth/api-keys", headers=auth, json={"name": "proto"})).json()["key"]


def asgi_client(app, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", **kw)


async def rpc(client, key, method, params=None, id_=1, path="/mcp"):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})


# ------------------------------------------------------------------ MCP server
async def test_mcp_server_lifecycle(client, auth, api_key):
    r = await rpc(
        client,
        api_key,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    )
    assert r.status_code == 200 and r.headers.get("mcp-session-id")
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "argus-council"
    assert (await rpc(client, api_key, "initialize", {"protocolVersion": "1999-01-01"})).json()["result"][
        "protocolVersion"
    ] == "2025-06-18"
    n = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert n.status_code == 202
    tools = {t["name"] for t in (await rpc(client, api_key, "tools/list")).json()["result"]["tools"]}
    assert {"start_run", "get_run", "list_runs", "intervene", "cancel_run", "list_councils"} <= tools

    r = await rpc(
        client,
        api_key,
        "tools/call",
        {"name": "start_run", "arguments": {"topic": "MCP topic", "mode": "chat", "wait_seconds": 15}},
    )
    result = r.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["status"] == "completed"
    run_id = result["structuredContent"]["run_id"]
    got = (
        await rpc(client, api_key, "tools/call", {"name": "get_run", "arguments": {"run_id": run_id}})
    ).json()
    assert got["result"]["structuredContent"]["result"].startswith("# MCP topic")
    bad = (
        await rpc(client, api_key, "tools/call", {"name": "get_run", "arguments": {"run_id": "nope"}})
    ).json()
    assert bad["error"]["code"] == -32602
    invalid = (await rpc(client, api_key, "tools/call", {"name": "start_run", "arguments": {}})).json()
    assert invalid["result"]["isError"] is True

    resources = (await rpc(client, api_key, "resources/list")).json()["result"]["resources"]
    uri = next(r["uri"] for r in resources if run_id in r["uri"])
    read = (await rpc(client, api_key, "resources/read", {"uri": uri})).json()["result"]["contents"][0]
    assert read["text"].startswith("# MCP topic")

    prompts = {p["name"] for p in (await rpc(client, api_key, "prompts/list")).json()["result"]["prompts"]}
    assert "research-report" in prompts
    p = (
        await rpc(client, api_key, "prompts/get", {"name": "code-review", "arguments": {"task": "x.py"}})
    ).json()
    assert "## Task\nx.py" in p["result"]["messages"][0]["content"]["text"]

    ledger = (await client.get("/api/v1/ledger", headers=auth, params={"protocol": "mcp"})).json()["entries"]
    assert any(e["method"] == "tools/call:start_run" and e["direction"] == "inbound" for e in ledger)
    assert (await client.get("/api/v1/ledger/verify", headers=auth)).json()["ok"] is True


async def test_mcp_server_errors(client, api_key):
    assert (await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
    r = await client.post("/mcp", content=b"{bad", headers={"Authorization": f"Bearer {api_key}"})
    assert r.json()["error"]["code"] == -32700
    r = await client.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 400
    assert (await rpc(client, api_key, "nope/method")).json()["error"]["code"] == -32601
    r = await client.post(
        "/mcp",
        json={"jsonrpc": "1.0", "id": 1, "method": "ping"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.json()["error"]["code"] == -32600
    assert (await rpc(client, api_key, "ping")).json()["result"] == {}
    assert (await client.get("/mcp")).status_code == 405
    r = await rpc(client, api_key, "resources/read", {"uri": "file:///etc/passwd"})
    assert r.json()["error"]["code"] == -32602


async def test_mcp_server_isolation(client, auth, api_key):
    from tests.conftest import register_and_login

    r = await rpc(
        client, api_key, "tools/call", {"name": "start_run", "arguments": {"topic": "mine", "mode": "chat"}}
    )
    run_id = r.json()["result"]["structuredContent"]["run_id"]
    other = await register_and_login(client, "mallory@example.com")
    other_key = (await client.post("/api/v1/auth/api-keys", headers=other, json={"name": "k"})).json()["key"]
    r = await rpc(client, other_key, "tools/call", {"name": "get_run", "arguments": {"run_id": run_id}})
    assert r.json()["error"]["message"] == "Run not found"
    await wait_status(client, auth, run_id)


# ------------------------------------------------------------------ MCP clients
async def test_http_mcp_client_against_argus(app, api_key):
    mcp = HttpMcpClient(
        "http://test/mcp", headers={"Authorization": f"Bearer {api_key}"}, client=asgi_client(app)
    )
    info = await mcp.initialize()
    assert info["serverInfo"]["name"] == "argus-council" and mcp.session_id
    names = [t["name"] for t in await mcp.list_tools()]
    assert "start_run" in names
    text, is_error = render_result(await mcp.call_tool("list_councils", {}))
    assert not is_error and json.loads(text) == {"councils": []}
    await mcp.aclose()


async def test_http_mcp_client_sse_and_errors():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "tools/call":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "boom"}}
            )
        sse = (
            f'event: message\ndata: {{"jsonrpc":"2.0","method":"notifications/progress"}}\n\n'
            f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': {'tools': [{'name': 'x'}]}})}\n\n"
        )
        return httpx.Response(
            200, text=sse, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"}
        )

    mcp = HttpMcpClient("http://x/mcp", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await mcp.list_tools() == [{"name": "x"}]
    assert mcp.session_id == "s1"
    with pytest.raises(McpClientError, match="boom"):
        await mcp.call_tool("x", {})


STDIO_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m = msg["method"]
        if m == "initialize":
            res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "echo"}}
        elif m == "tools/list":
            res = {"tools": [{"name": "echo", "description": "Echo text",
                              "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
        elif m == "tools/call":
            res = {"content": [{"type": "text", "text": "echo:" + msg["params"]["arguments"].get("text", "")}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}),
                  flush=True)
            continue
        print("log noise on stdout", flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
""")


async def test_stdio_mcp_client(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(STDIO_SERVER)
    mcp = StdioMcpClient([sys.executable, str(script)], timeout=10)
    try:
        assert (await mcp.initialize())["serverInfo"]["name"] == "echo"
        assert [t["name"] for t in await mcp.list_tools()] == ["echo"]
        text, err = render_result(await mcp.call_tool("echo", {"text": "hi"}))
        assert text == "echo:hi" and not err
    finally:
        await mcp.aclose()


def test_tool_name_and_render():
    assert tool_name("my server", "do.thing!") == "mcp__my_server__do_thing_"
    assert len(tool_name("s" * 50, "t" * 50)) == 64
    text, err = render_result(
        {"content": [{"type": "image"}, {"type": "resource", "resource": {"text": "R"}}], "isError": True}
    )
    assert text == "[image content omitted]\nR" and err


# ------------------------------------------------------------------ A2A
async def test_a2a_card_and_tasks(client, app, api_key):
    card = (await client.get("/.well-known/agent-card.json")).json()
    assert card["url"].endswith("/a2a") and {s["id"] for s in card["skills"]} == {
        "research",
        "coding",
        "chat",
    }
    assert (await client.get("/.well-known/agent.json")).json()["name"] == "Argus Council"

    a2a = A2AClient("http://test/a2a", api_key, client=asgi_client(app))
    done = await a2a.run_to_completion("Explain A2A\nKeep it short", metadata={"mode": "chat"}, poll_s=0.05)
    assert done["status"]["state"] == "completed" and task_text(done).startswith("# Explain A2A")
    again = await a2a.get_task(done["id"])
    assert again["id"] == done["id"]
    with pytest.raises(Exception, match="cannot be canceled"):
        await a2a.cancel(done["id"])
    with pytest.raises(Exception, match="Task not found"):
        await a2a.get_task("00000000-0000-0000-0000-000000000000")
    blocking = await a2a.send("Quick one", metadata={"mode": "chat"}, blocking=True)
    assert blocking["status"]["state"] == "completed"
    await a2a.aclose()


async def test_a2a_errors(client, api_key):
    assert (await rpc(client, "", "message/send", path="/a2a")).status_code == 401
    r = await rpc(client, api_key, "message/send", {"message": {"parts": []}}, path="/a2a")
    assert r.json()["error"]["code"] == -32602
    assert (await rpc(client, api_key, "tasks/resubscribe", {}, path="/a2a")).json()["error"][
        "code"
    ] == -32601


# ------------------------------------------------------------------ ACP
async def test_acp_agents_and_runs(client, auth, app, api_key):
    await client.post(
        "/api/v1/councils", headers=auth, json={"name": "Deep Dive", "mode": "research", "size": 4}
    )
    acp = AcpClient("http://test/acp", api_key, client=asgi_client(app))
    names = {a["name"] for a in await acp.agents()}
    assert {"argus-research", "argus-coding", "argus-chat", "council-deep-dive"} <= names
    run = await acp.run("argus-chat", "What is ACP?", poll_s=0.05)
    assert run["status"] == "completed" and run_text(run).startswith("# What is ACP?")
    sync = await client.post(
        "/acp/runs",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "agent_name": "council-deep-dive",
            "mode": "sync",
            "input": [
                {"role": "user", "parts": [{"content_type": "text/plain", "content": "Battery recycling"}]}
            ],
        },
    )
    assert sync.json()["status"] == "completed" and sync.json()["output"][0]["parts"][0]["content"]
    assert (await client.get("/acp/agents/nope", headers=auth)).status_code == 404
    r = await client.post(
        "/acp/runs",
        headers=auth,
        json={"agent_name": "argus-chat", "mode": "stream", "input": [{"parts": [{"content": "x"}]}]},
    )
    assert r.status_code == 400
    await acp.aclose()
    ledger = (await client.get("/api/v1/ledger", headers=auth, params={"protocol": "acp"})).json()["entries"]
    assert {e["method"] for e in ledger} >= {"runs:async", "runs:sync"}


# ------------------------------------------------------------------ connections + agents using them
async def test_connections_api_validation(client, auth):
    r = await client.post(
        "/api/v1/mcp-servers",
        headers=auth,
        json={"name": "local", "transport": "stdio", "command": ["npx", "server"]},
    )
    assert r.status_code == 403  # stdio disabled by default
    assert (await client.post("/api/v1/mcp-servers", headers=auth, json={"name": "x"})).status_code == 400
    r = await client.post(
        "/api/v1/mcp-servers",
        headers=auth,
        json={
            "name": "files",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer s3cret"},
        },
    )
    assert r.status_code == 201 and r.json()["header_names"] == ["Authorization"] and "s3cret" not in r.text
    assert (
        await client.post(
            "/api/v1/remote-agents",
            headers=auth,
            json={"name": "a", "protocol": "acp", "url": "https://x.io"},
        )
    ).status_code == 422
    r = await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={
            "name": "helper",
            "protocol": "a2a",
            "url": "https://agents.example.com/a2a",
            "token": "zq-secret-91",
        },
    )
    assert r.status_code == 201 and r.json()["has_token"] is True and "zq-secret-91" not in r.text
    assert (
        await client.post(
            "/api/v1/remote-agents",
            headers=auth,
            json={"name": "helper", "protocol": "a2a", "url": "https://x.io"},
        )
    ).status_code == 409


@pytest.fixture
def loopback(app, monkeypatch):
    """Route outbound MCP/A2A/ACP HTTP from the server back into the same app."""
    import argus.services.connections as conns
    import argus.tools.remote as remote

    class LoopMcp(HttpMcpClient):
        def __init__(self, url, headers=None, timeout=60.0, client=None):
            super().__init__(url, headers=headers, timeout=timeout, client=asgi_client(app))

    class LoopA2A(A2AClient):
        def __init__(self, url, token=None, timeout=60.0, client=None):
            super().__init__(url, token, timeout, client=asgi_client(app))

        async def run_to_completion(self, text, metadata=None, timeout_s=600, poll_s=2.0):
            return await super().run_to_completion(text, {"mode": "chat"}, timeout_s, 0.05)

    monkeypatch.setattr(conns, "HttpMcpClient", LoopMcp)
    monkeypatch.setattr(remote, "A2AClient", LoopA2A)


async def test_agents_use_external_mcp_and_remote_agent(client, auth, app, api_key, loopback):
    r = await client.post(
        "/api/v1/mcp-servers",
        headers=auth,
        json={
            "name": "self",
            "url": "http://test/mcp",
            "headers": {"Authorization": f"Bearer {api_key}"},
            "tool_allow": ["list_*"],
        },
    )
    sid = r.json()["id"]
    d = await client.post(f"/api/v1/mcp-servers/{sid}/discover", headers=auth)
    assert d.status_code == 200 and {t["name"] for t in d.json()["tools"]} == {"list_runs", "list_councils"}
    await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={
            "name": "twin",
            "protocol": "a2a",
            "url": "http://test/a2a",
            "token": api_key,
            "description": "Argus twin",
        },
    )

    seen_tools = []

    def responder(req):
        if req.metadata.get("phase") == "execute":
            seen_tools.append({t.name for t in req.tools})
            done = [m for m in req.messages if m.role == "tool"]
            if not done:
                return {"content": "", "tool_calls": [{"name": "mcp__self__list_councils", "arguments": {}}]}
            if len(done) == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {"name": "remote_agent", "arguments": {"agent": "twin", "message": "Nested question"}}
                    ],
                }
            return {"content": "Results:\n" + "\n---\n".join(m.content for m in done)}
        return council_responder(req)

    app.state.llm.system["proto"] = MockProvider("proto", responder=responder)
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={
            "topic": "outer",
            "mode": "chat",
            "size": 2,
            "provider": "proto",
            "model": "m",
            "guardrails": {"approval_required": []},
        },
    )
    run = await wait_status(client, auth, r.json()["id"], timeout=30)
    assert run["status"] == "completed", run
    assert {"mcp__self__list_councils", "mcp__self__list_runs", "remote_agent"} <= seen_tools[0]
    assert "mcp__self__start_run" not in seen_tools[0]  # filtered by tool_allow
    evs = await events_of(client, auth, run["id"])
    done = [e["data"] for e in evs if e["type"] == "tool.call.completed"]
    assert [d["status"] for d in done] == ["ok", "ok"]
    assert '"councils"' in done[0]["result_preview"] and "# Nested question" in done[1]["result_preview"]
    started = [e["data"] for e in evs if e["type"] == "tool.call.started"]
    assert started[0]["source"] == "mcp" and started[1]["source"] == "a2a"

    entries = (await client.get("/api/v1/ledger", headers=auth)).json()["entries"]
    kinds = {(e["protocol"], e["direction"], e["method"]) for e in entries}
    assert ("mcp", "outbound", "tools/call:list_councils") in kinds
    assert ("mcp", "inbound", "tools/call:list_councils") in kinds
    assert ("a2a", "outbound", "message/send") in kinds and ("a2a", "inbound", "message/send") in kinds
    outbound = [e for e in entries if e["direction"] == "outbound"]
    # Tool calls belong to the run. MCP discovery is ledgered too: via the API outside any run, and
    # at attach time under the run that connected (the outer run or the A2A twin's nested run).
    calls = [e for e in outbound if e["method"].startswith("tools/call") or e["method"] == "message/send"]
    assert calls and all(e["run_id"] == run["id"] for e in calls)
    attach = [e for e in outbound if e["detail"].get("purpose") == "run"]
    assert {e["method"] for e in attach} == {"initialize", "tools/list"} and all(e["run_id"] for e in attach)
    assert any(e["run_id"] == run["id"] for e in attach)
    assert {e["method"] for e in outbound if e["detail"].get("purpose") == "discover"} == {
        "initialize",
        "tools/list",
    }
    assert (await client.get("/api/v1/ledger/verify", headers=auth)).json()["ok"] is True


async def test_dead_mcp_server_does_not_block_run(client, auth, app):
    await client.post(
        "/api/v1/mcp-servers", headers=auth, json={"name": "dead", "url": "http://dead-mcp.invalid/mcp"}
    )
    r = await client.post("/api/v1/runs", headers=auth, json={"topic": "t", "mode": "chat", "size": 2})
    run = await wait_status(client, auth, r.json()["id"], timeout=90)
    assert run["status"] == "completed"
    errors = [e for e in await events_of(client, auth, run["id"]) if e["type"] == "error"]
    assert errors and errors[0]["data"]["code"] == "mcp_unavailable"


async def test_ledger_tamper_detection(client, auth, app, api_key):
    from sqlalchemy import update

    from argus.db.models import LedgerEntry

    for _ in range(3):
        await rpc(client, api_key, "tools/list")
    assert (await client.get("/api/v1/ledger/verify", headers=auth)).json() == {
        "ok": True,
        "entries": 3,
        "head": (await client.get("/api/v1/ledger", headers=auth)).json()["entries"][0]["hash"],
    }
    async with app.state.db.sessionmaker() as s:
        await s.execute(update(LedgerEntry).where(LedgerEntry.seq == 2).values(status="tampered"))
        await s.commit()
    v = (await client.get("/api/v1/ledger/verify", headers=auth)).json()
    assert v["ok"] is False and v["broken_at"] == 2
