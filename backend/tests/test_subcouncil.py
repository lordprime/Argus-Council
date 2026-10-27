import uuid

from argus.db.models import Run
from argus.providers.mock import MockProvider, council_responder
from argus.providers.types import CompletionResponse, ToolCall, Usage
from tests.test_protocols import api_key, loopback  # noqa: F401  (fixtures)
from tests.test_runs import events_of, wait_status


def _responder(req):
    out = council_responder(req)
    if req.metadata.get("phase") == "execute" and "subcouncil" in {t.name for t in req.tools or []}:
        if not any(m.role == "tool" for m in req.messages):
            out = {
                "content": "",
                "tool_calls": [
                    {
                        "name": "subcouncil",
                        "arguments": {"task": "Survey the sub-topic", "size": 2, "mode": "chat"},
                    }
                ],
            }
        else:
            out = {"content": "merged the sub-council's findings"}
    return CompletionResponse(
        provider="sub",
        model="m",
        content=out.get("content", ""),
        tool_calls=[ToolCall(id=f"call_{i}", **c) for i, c in enumerate(out.get("tool_calls") or [])],
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01),
    )


async def test_member_delegates_to_subcouncil(client, auth, app):
    app.state.llm.system["sub"] = MockProvider("sub", responder=_responder)
    council = {
        "name": "delegating",
        "mode": "chat",
        "guardrails": {"max_subcouncil_depth": 1},
        "members": [
            {"name": "Chair", "role": "chair", "provider": "sub", "model": "m"},
            {
                "name": "Lead",
                "role": "worker",
                "provider": "sub",
                "model": "m",
                "config": {"tools": ["subcouncil"]},
            },
        ],
    }
    c = await client.post("/api/v1/councils", headers=auth, json=council)
    assert c.status_code == 201, c.text
    run = (
        await client.post(
            "/api/v1/runs", headers=auth, json={"topic": "big task", "council_id": c.json()["id"]}
        )
    ).json()
    assert (await wait_status(client, auth, run["id"], timeout=30))["status"] == "completed"

    evs = await events_of(client, auth, run["id"])
    started = next(e for e in evs if e["type"] == "subcouncil.started")
    done = next(e for e in evs if e["type"] == "subcouncil.completed")
    child_id = started["data"]["child_run_id"]
    assert done["data"]["child_run_id"] == child_id and done["data"]["status"] == "completed"
    assert len(started["data"]["members"]) == 2 and started["data"]["depth"] == 1
    # Relayed child activity is live-only and never stored in the parent's history.
    assert not any(e["type"] == "subcouncil.event" for e in evs)

    async with app.state.db.sessionmaker() as s:
        child = await s.get(Run, uuid.UUID(child_id))
        parent = await s.get(Run, uuid.UUID(run["id"]))
    assert child.parent_run_id == parent.id and child.status == "completed"
    # Guardrails are clamped: the child may not spawn further sub-councils.
    assert child.guardrails["max_subcouncil_depth"] == 0
    # The child's spend is charged to the parent.
    assert parent.usage["usd"] >= child.usage["usd"] > 0
    # Child runs stay out of the top-level runs list.
    listed = (await client.get("/api/v1/runs", headers=auth)).json()
    assert child_id not in {r["id"] for r in listed}


async def test_subcouncil_refused_when_depth_exhausted(client, auth, app):
    app.state.llm.system["sub"] = MockProvider("sub", responder=_responder)
    council = {
        "name": "flat",
        "mode": "chat",
        "guardrails": {"max_subcouncil_depth": 0},
        "members": [
            {"name": "Chair", "role": "chair", "provider": "sub", "model": "m"},
            {
                "name": "Lead",
                "role": "worker",
                "provider": "sub",
                "model": "m",
                "config": {"tools": ["subcouncil"]},
            },
        ],
    }
    c = (await client.post("/api/v1/councils", headers=auth, json=council)).json()
    run = (await client.post("/api/v1/runs", headers=auth, json={"topic": "t", "council_id": c["id"]})).json()
    assert (await wait_status(client, auth, run["id"], timeout=30))["status"] == "completed"
    evs = await events_of(client, auth, run["id"])
    assert not any(e["type"] == "subcouncil.started" for e in evs)
    done = [e for e in evs if e["type"] == "tool.call.completed" and e["data"].get("status") == "error"]
    assert done, "the refused delegation should surface as a tool error"


async def test_protocol_calls_are_announced_on_the_run_stream(client, auth, app, api_key, loopback):  # noqa: F811
    sid = (
        await client.post(
            "/api/v1/mcp-servers",
            headers=auth,
            json={
                "name": "self",
                "url": "http://test/mcp",
                "headers": {"Authorization": f"Bearer {api_key}"},
                "tool_allow": ["list_*"],
            },
        )
    ).json()["id"]
    assert (await client.post(f"/api/v1/mcp-servers/{sid}/discover", headers=auth)).status_code == 200
    await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={"name": "twin", "protocol": "a2a", "url": "http://test/a2a", "token": api_key},
    )

    def responder(req):
        if req.metadata.get("phase") == "execute":
            done = [m for m in req.messages if m.role == "tool"]
            if not done:
                return {"content": "", "tool_calls": [{"name": "mcp__self__list_councils", "arguments": {}}]}
            if len(done) == 1:
                call = {"name": "remote_agent", "arguments": {"agent": "twin", "message": "Nested"}}
                return {"content": "", "tool_calls": [call]}
            return {"content": "done"}
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
    evs = await events_of(client, auth, run["id"])
    calls = [e["data"] for e in evs if e["type"] == "protocol.call"]
    assert [(c["protocol"], c["direction"]) for c in calls] == [("mcp", "outbound"), ("a2a", "outbound")]
    tool_ids = {e["data"]["tool_call_id"] for e in evs if e["type"] == "tool.call.started"}
    assert all(c["ledger_id"] and c["hash"] and c["tool_call_id"] in tool_ids for c in calls)
    assert calls[0]["method"] == "tools/call:list_councils" and calls[1]["method"] == "message/send"
    entries = (await client.get("/api/v1/ledger", headers=auth)).json()["entries"]
    assert {c["hash"] for c in calls} <= {e["hash"] for e in entries}
