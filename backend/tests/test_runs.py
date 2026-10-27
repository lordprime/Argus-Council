import asyncio
import json
import uuid

import pytest
from pydantic import BaseModel

from argus.providers.mock import MockProvider, council_responder
from argus.tools.base import Tool, ToolRegistry, ToolResult
from argus.tools.builtin import builtin_tools

API = "/api/v1/runs"


async def wait_status(
    client, auth, run_id, statuses=("completed", "failed", "halted", "cancelled"), timeout=15
):
    for _ in range(int(timeout / 0.05)):
        r = (await client.get(f"{API}/{run_id}", headers=auth)).json()
        if r["status"] in statuses:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"run stuck in {r['status']}")


async def events_of(client, auth, run_id):
    return (await client.get(f"{API}/{run_id}/events", headers=auth)).json()["events"]


def types(evs):
    return [e["type"] for e in evs]


def install(app, name, responder):
    app.state.llm.system[name] = MockProvider(name, responder=responder)


async def test_offline_research_run_end_to_end(client, auth, app):
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "Solid-state batteries", "mode": "research", "outputs": ["md", "pdf"]},
    )
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    assert run["usage"]["steps"] > 0 and run["usage"]["llm_calls"] > 0
    evs = await events_of(client, auth, run["id"])
    t = types(evs)
    assert t[0] == "run.status" and t[-1] == "run.status" and evs[-1]["data"]["status"] == "completed"
    for needed in (
        "run.plan",
        "step.status",
        "thought.started",
        "thought.completed",
        "vote.opened",
        "vote.cast",
        "vote.closed",
        "artifact.upserted",
    ):
        assert needed in t, needed
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    plan = next(e for e in evs if e["type"] == "run.plan")["data"]
    assert [s["title"] for s in plan["steps"]][:2] == ["Scope the questions", "Gather evidence"]
    detail = (await client.get(f"{API}/{run['id']}", headers=auth)).json()
    assert detail["result"].startswith("# Solid-state batteries")
    arts = (await client.get(f"{API}/{run['id']}/artifacts", headers=auth)).json()["artifacts"]
    assert [a["path"] for a in arts] == ["exports/solid-state-batteries.pdf", "report.md"]
    content = await client.get(
        f"{API}/{run['id']}/artifacts/content", params={"path": "report.md"}, headers=auth
    )
    assert content.text == detail["result"] and content.headers["content-type"].startswith("text/plain")
    snap = (await client.get(f"{API}/{run['id']}/snapshot", headers=auth)).json()
    assert snap["status"] == "completed" and all(s["status"] == "done" for s in snap["steps"].values())
    assert snap["budget"]["used"]["steps"] == run["usage"]["steps"]


async def test_run_with_saved_council_and_guardrail_layers(client, auth):
    c = (
        await client.post(
            "/api/v1/councils",
            headers=auth,
            json={"name": "chatters", "mode": "chat", "size": 2, "guardrails": {"max_steps": 40}},
        )
    ).json()
    r = await client.post(
        API, headers=auth, json={"topic": "hi", "council_id": c["id"], "guardrails": {"budget": {"usd": 1}}}
    )
    run = r.json()
    assert (
        run["mode"] == "chat"
        and run["guardrails"]["max_steps"] == 40
        and run["guardrails"]["budget"]["usd"] == 1
    )
    assert (await wait_status(client, auth, run["id"]))["status"] == "completed"


async def test_invalid_guardrails_rejected(client, auth):
    r = await client.post(API, headers=auth, json={"topic": "x", "guardrails": {"max_stepz": 1}})
    assert r.status_code == 400 and r.json()["error"]["message"] == "Invalid guardrails"


async def test_overseer_hold_forces_correction(client, auth, app):
    reviews = {"n": 0}

    def responder(req):
        meta = req.metadata
        if (
            meta.get("phase") == "review"
            and meta.get("role") == "monitor"
            and "cats" in req.messages[-1].content
        ):
            reviews["n"] += 1
            if reviews["n"] == 1:
                return {
                    "content": json.dumps(
                        {
                            "action": "hold",
                            "reason": "drift",
                            "severity": "medium",
                            "note": "off topic",
                            "correction": "Focus on the topic.",
                        }
                    )
                }
        if meta.get("phase") == "execute":
            corrected = any("Focus on the topic" in m.content for m in req.messages)
            return {"content": "on-topic answer" if corrected else "something about cats"}
        return council_responder(req)

    install(app, "scripted", responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Rust ownership",
            "mode": "chat",
            "size": 2,
            "provider": "scripted",
            "model": "m",
            "guardrails": {"oversight": {"monitor_sensitivity": "high"}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed"
    evs = await events_of(client, auth, run["id"])
    intercept = next(e for e in evs if e["type"] == "intercept.raised")
    assert intercept["data"]["action"] == "hold" and intercept["actor"]["role"] == "monitor"
    corr = next(e for e in evs if e["type"] == "correction.issued")
    assert corr["causation_id"] == intercept["id"] and corr["data"]["text"] == "Focus on the topic."
    step_done = [e for e in evs if e["type"] == "step.status" and e["data"]["status"] == "done"]
    assert step_done[0]["data"]["summary"] == "on-topic answer"


async def test_intercept_mode_annotate_caps_overseers(client, auth, app):
    def responder(req):
        if req.metadata.get("phase") == "review":
            return {
                "content": json.dumps(
                    {"action": "block", "reason": "factual", "note": "dubious", "correction": "double-check"}
                )
            }
        return council_responder(req)

    install(app, "blocky", responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "t",
            "mode": "chat",
            "size": 2,
            "provider": "blocky",
            "model": "m",
            "guardrails": {"oversight": {"intercept_mode": "annotate"}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed"
    actions = {
        e["data"]["action"]
        for e in await events_of(client, auth, run["id"])
        if e["type"] == "intercept.raised"
    }
    assert actions == {"annotate"}


async def test_loop_detection_blocks_repeated_tool_calls(client, auth, app):
    def responder(req):
        if req.metadata.get("phase") == "execute":
            return {"content": "", "tool_calls": [{"name": "notes_read", "arguments": {"query": "same"}}]}
        return council_responder(req)

    install(app, "loopy", responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "t",
            "mode": "chat",
            "size": 2,
            "provider": "loopy",
            "model": "m",
            "guardrails": {"oversight": {"loop_threshold": 3, "max_corrections_per_thought": 1}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    evs = await events_of(client, auth, run["id"])
    loops = [e for e in evs if e["type"] == "intercept.raised" and e["data"]["reason"] == "loop"]
    assert loops and loops[0]["actor"]["name"] == "Loop detector"
    failed = [e for e in evs if e["type"] == "step.status" and e["data"]["status"] == "failed"]
    assert failed and failed[0]["data"]["reason"] == "blocked"


async def test_budget_halts_run(client, auth, app):
    from argus.providers.types import CompletionResponse, Usage

    def responder(req):
        out = council_responder(req)
        return CompletionResponse(
            provider="pricey",
            model="m",
            content=out["content"],
            usage=Usage(input_tokens=10, output_tokens=10, cost_usd=0.4),
        )

    install(app, "pricey", responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "t",
            "mode": "research",
            "provider": "pricey",
            "model": "m",
            "guardrails": {"budget": {"usd": 1.0}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "halted" and "budget" in run["error"].lower()
    v = [e for e in await events_of(client, auth, run["id"]) if e["type"] == "guardrail.violation"]
    assert v[-1]["data"]["rule"] == "budget.usd" and v[-1]["data"]["action"] == "halted"


class RiskyTool(Tool):
    name = "deploy"
    description = "Pretend to deploy"
    risk = frozenset({"shell"})
    calls: list = []

    class Args(BaseModel):
        target: str

    async def run(self, args, ctx):
        RiskyTool.calls.append(args.target)
        return ToolResult(f"deployed to {args.target}")


async def _tool_council(client, auth, provider, guardrails):
    c = (
        await client.post(
            "/api/v1/councils",
            headers=auth,
            json={
                "name": f"ops-{provider}",
                "mode": "chat",
                "guardrails": guardrails,
                "members": [
                    {
                        "name": "Ops",
                        "role": "assistant",
                        "provider": provider,
                        "model": "m",
                        "config": {"tools": ["*"]},
                    },
                    {
                        "name": "Watch",
                        "role": "monitor",
                        "provider": provider,
                        "model": "m",
                        "config": {"overseer": True},
                    },
                ],
            },
        )
    ).json()
    return c["id"]


def _tool_run_setup(app, provider):
    RiskyTool.calls = []
    app.state.runs.tool_factory = lambda run: ToolRegistry([*builtin_tools(), RiskyTool()])

    def responder(req):
        if req.metadata.get("phase") == "execute":
            if any(m.role == "tool" for m in req.messages):
                return {"content": "done: " + next(m.content for m in req.messages if m.role == "tool")}
            return {"content": "", "tool_calls": [{"name": "deploy", "arguments": {"target": "staging"}}]}
        return council_responder(req)

    install(app, provider, responder)


async def _wait_for_event(client, auth, run_id, type_, timeout=10):
    for _ in range(int(timeout / 0.05)):
        for e in await events_of(client, auth, run_id):
            if e["type"] == type_:
                return e
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {type_}")


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_approval_gate(client, auth, app, decision):
    _tool_run_setup(app, "tooly")
    cid = await _tool_council(client, auth, "tooly", {"oversight": {"monitor_sensitivity": "off"}})
    r = await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})
    rid = r.json()["id"]
    req = await _wait_for_event(client, auth, rid, "approval.requested")
    assert req["data"]["risk"] == ["shell"] and RiskyTool.calls == []
    snap = (await client.get(f"{API}/{rid}/snapshot", headers=auth)).json()
    assert len(snap["open_approvals"]) == 1
    edited = {"edited_args": {"target": "prod"}} if decision == "approve" else {}
    d = await client.post(
        f"{API}/{rid}/approvals/{req['data']['approval_id']}",
        headers=auth,
        json={"decision": decision, **edited},
    )
    assert d.status_code == 202
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed"
    if decision == "approve":
        assert RiskyTool.calls == ["prod"]
    else:
        assert RiskyTool.calls == []
        done = [e for e in await events_of(client, auth, rid) if e["type"] == "tool.call.completed"]
        assert done[0]["data"]["status"] == "blocked"
    again = await client.post(
        f"{API}/{rid}/approvals/{req['data']['approval_id']}", headers=auth, json={"decision": "approve"}
    )
    assert again.status_code in (404, 409)


async def test_tool_denied_by_guardrails(client, auth, app):
    _tool_run_setup(app, "tooly2")
    cid = await _tool_council(client, auth, "tooly2", {"tools": {"deny": ["deploy"]}})
    r = await client.post(API, headers=auth, json={"topic": "t", "council_id": cid})
    run = await wait_status(client, auth, r.json()["id"])
    evs = await events_of(client, auth, run["id"])
    assert any(e["type"] == "guardrail.violation" and e["data"]["rule"] == "tools.deny" for e in evs)
    assert RiskyTool.calls == []


async def test_user_interventions_pause_correct_cancel(client, auth, app):
    gate = asyncio.Event()

    async def slow_complete(self, request):
        await gate.wait()
        return MockProvider._build(self, request, council_responder(request))

    class Slow(MockProvider):
        complete = slow_complete

    app.state.llm.system["slow"] = Slow("slow")
    r = await client.post(
        API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2, "provider": "slow", "model": "m"}
    )
    rid = r.json()["id"]
    await _wait_for_event(client, auth, rid, "thought.started")
    members = (await client.get(f"{API}/{rid}", headers=auth)).json()["council_snapshot"]["members"]
    assistant = next(m for m in members if m["role"] == "assistant")

    assert (await client.post(f"{API}/{rid}/pause", headers=auth)).status_code == 202
    assert (await client.get(f"{API}/{rid}", headers=auth)).json()["status"] == "paused"
    res = await client.post(
        f"{API}/{rid}/interventions",
        headers=auth,
        json={
            "kind": "correct",
            "target": {"member_id": assistant["id"]},
            "text": "Use bullet points",
            "scope": "member",
        },
    )
    assert res.status_code == 202 and res.json()["correction_id"].startswith("cor_")
    res = await client.post(
        f"{API}/{rid}/interventions", headers=auth, json={"kind": "message", "text": "Also mention costs"}
    )
    assert res.status_code == 202
    bad = await client.post(
        f"{API}/{rid}/interventions",
        headers=auth,
        json={"kind": "hold", "target": {"member_id": str(uuid.uuid4())}},
    )
    assert bad.status_code == 404
    assert (
        await client.post(f"{API}/{rid}/interventions", headers=auth, json={"kind": "correct"})
    ).status_code == 422
    assert (await client.post(f"{API}/{rid}/resume", headers=auth)).status_code == 202
    assert (await client.post(f"{API}/{rid}/cancel", headers=auth)).status_code == 202
    gate.set()
    run = await wait_status(client, auth, rid)
    assert run["status"] == "cancelled"
    t = types(await events_of(client, auth, rid))
    assert "correction.issued" in t and "user.message" in t
    assert (await client.post(f"{API}/{rid}/pause", headers=auth)).status_code == 409
    assert (await client.delete(f"{API}/{rid}", headers=auth)).status_code == 204


async def test_standing_order_reaches_prompt(client, auth, app):
    seen = []
    gate = asyncio.Event()

    async def responder_async(self, request):
        if request.metadata.get("phase") == "execute":
            await gate.wait()
        if gate.is_set():
            seen.append(request.messages[0].content)
        return MockProvider._build(self, request, council_responder(request))

    class Gated(MockProvider):
        complete = responder_async

    app.state.llm.system["gated"] = Gated("gated")
    r = await client.post(
        API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2, "provider": "gated", "model": "m"}
    )
    rid = r.json()["id"]
    await _wait_for_event(client, auth, rid, "run.plan")
    await client.post(
        f"{API}/{rid}/interventions",
        headers=auth,
        json={"kind": "correct", "text": "Always answer in French", "scope": "council"},
    )
    gate.set()
    await wait_status(client, auth, rid)
    assert any("Always answer in French" in s for s in seen)


async def test_sse_stream_replays_and_follows(client, auth):
    r = await client.post(API, headers=auth, json={"topic": "stream me", "mode": "chat", "size": 2})
    rid = r.json()["id"]
    ticket = (await client.post(f"{API}/{rid}/stream-ticket", headers=auth)).json()["ticket"]
    frames = []
    async with client.stream("GET", f"{API}/{rid}/events/stream", params={"ticket": ticket}) as resp:
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
    durable = [f for f in frames if not f.get("ephemeral")]
    assert [f["seq"] for f in durable] == list(range(1, len(durable) + 1))
    assert durable[-1]["type"] == "run.status" and durable[-1]["data"]["status"] == "completed"
    # ticket is single use
    again = await client.get(f"{API}/{rid}/events/stream", params={"ticket": ticket})
    assert again.status_code == 401
    # resume from a seq with bearer auth
    async with client.stream(
        "GET", f"{API}/{rid}/events/stream", params={"after_seq": len(durable) - 2}, headers=auth
    ) as resp:
        tail = [json.loads(line[6:]) async for line in resp.aiter_lines() if line.startswith("data: ")]
    assert [f["seq"] for f in tail] == [len(durable) - 1, len(durable)]


async def test_run_isolation(client, auth):
    from tests.conftest import register_and_login

    rid = (await client.post(API, headers=auth, json={"topic": "secret", "mode": "chat", "size": 2})).json()[
        "id"
    ]
    other = await register_and_login(client, "eve@example.com")
    for path in ("", "/events", "/snapshot", "/artifacts"):
        assert (await client.get(f"{API}/{rid}{path}", headers=other)).status_code == 404
    assert (await client.post(f"{API}/{rid}/cancel", headers=other)).status_code == 404
    assert (await client.post(f"{API}/{rid}/stream-ticket", headers=other)).status_code == 404
    assert (await client.get(f"{API}/{rid}/events/stream", headers=other)).status_code == 404
    assert (await client.get(API, headers=other)).json() == []
    await wait_status(client, auth, rid)
