"""Runtime sub-agents: helpers join the council, work beside it as A2A tasks, and leave."""

import asyncio
import inspect
import time
import uuid
from collections import defaultdict

import pytest
from sqlalchemy import select

from argus.db.models import LedgerEntry
from argus.providers.mock import MockProvider, council_responder
from argus.providers.types import StreamEvent, ToolCall
from argus.runtime.agent import MemberSpec
from argus.runtime.context import RunCancelled
from argus.runtime.orchestrator import CouncilOrchestrator, Step
from argus.runtime.resume import TASK_EVENT, ResumeState
from argus.tools.base import ToolRegistry
from argus.tools.builtin import builtin_tools
from argus.tools.spawn import HelperIn
from tests import test_limit_extension as limits
from tests.test_limit_extension import QUIET, make_ctx, reply
from tests.test_runs import API, _wait_for_event, events_of, wait_status

db, run_row = limits.db, limits.run_row  # the same fixtures

DELAY = 0.15


class SlowLLM:
    """Answers with ``script(request)`` (may be async); a helper's turn takes ``DELAY`` seconds and
    waits for ``gate`` when one is set. Records every request and when each helper turn ran."""

    def __init__(self, script, gate: asyncio.Event | None = None) -> None:
        self.script = script
        self.gate = gate
        self.requests = []
        self.spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.waiting = 0

    async def _respond(self, req):
        self.requests.append(req)
        if req.metadata.get("phase") == "spawn":
            t0 = time.monotonic()
            if self.gate is not None and not any(m.role == "tool" for m in req.messages):
                self.waiting += 1
                await self.gate.wait()
            await asyncio.sleep(DELAY)
            self.spans[req.metadata["member_id"]].append((t0, time.monotonic()))
        out = self.script(req)
        return await out if inspect.isawaitable(out) else out

    async def complete(self, req, route, **kw):
        return await self._respond(req)

    async def stream(self, req, route, **kw):
        yield StreamEvent(type="done", response=await self._respond(req))


def _member(mid, name, role):
    return MemberSpec(id=mid, name=name, role=role, provider="fake", model="m", system_prompt=f"The {role}.")


SPAWN = ToolCall(
    id="sp1",
    name="spawn_agents",
    arguments={
        "agents": [
            {"role": "researcher", "instructions": "Gather evidence on solid-state cells"},
            {"role": "analyst", "instructions": "Gather evidence on lithium-ion cells"},
        ],
        "wait": True,
    },
)


def _script(req):
    """Ada spawns two helpers on her first turn, then merges; helpers answer with their own name."""
    phase, member = req.metadata.get("phase"), req.metadata.get("member")
    tools_done = [m for m in req.messages if m.role == "tool"]
    if phase == "spawn":
        return reply(f"result from {member}")
    if phase == "execute" and member == "Ada":
        if not tools_done:
            return reply("splitting the comparison", [SPAWN])
        return reply(f"Ada merged: {tools_done[-1].content}")
    return reply("ok")


def _council(db, run_row, script=_script, rails=None, gate=None):
    llm = SlowLLM(script, gate)
    ctx, events = make_ctx(db, run_row, rails or QUIET, llm)
    members = [
        _member("m1", "Ada", "researcher"),
        _member("m2", "Ben", "analyst"),
        _member("m3", "Cy", "chair"),
    ]
    orch = CouncilOrchestrator(ctx, members, ToolRegistry(builtin_tools()))
    return orch, ctx, events, llm


def _of(events, type_):
    return [e.data for e in events if e.type == type_]


def _overlap(spans):
    return max(s for s, _ in spans) < min(e for _, e in spans)


async def test_helpers_join_the_council_work_at_the_same_time_and_leave(db, run_row):
    seen: dict[str, object] = {}

    def script(req):
        if req.metadata.get("phase") == "spawn":
            seen[req.metadata["member"]] = {
                "roster": [m.id for m in orch.members],
                "bus": set(orch.council.members),
                "system": req.messages[0].content,
                "tools": {t.name for t in req.tools},
            }
        return _script(req)

    orch, ctx, events, llm = _council(db, run_row, script)
    out = await orch.runner(orch.members[0]).run_task("Compare the cells", step_title="Gather")
    assert (
        out.ok
        and "result from Researcher helper 1" in out.text
        and "result from Analyst helper 2" in out.text
    )

    # Two helpers, with their own A2A cards, joined while they worked and ran at the same time.
    joined = _of(events, "member.joined")
    assert [j["member"]["name"] for j in joined] == ["Researcher helper 1", "Analyst helper 2"]
    assert all(j["member"]["ephemeral"] and j["member"]["parent_id"] == "m1" for j in joined)
    ids = [j["member"]["id"] for j in joined]
    assert all(j["card"]["url"].endswith(f"/agents/{j['member']['id']}") for j in joined)
    assert _overlap([llm.spans[i][0] for i in ids])
    for name, view in seen.items():
        assert set(ids) <= set(view["roster"]) and set(ids) <= view["bus"], name
        assert "## Your council (A2A agents)" in view["system"] and f"{name} (" in view["system"]
        # Teammates' work as it happens: Ada's latest thought is in "Working now".
        assert "## Working now" in view["system"] and "Ada (researcher) on 'Gather'" in view["system"]
        assert {"ask_agent", "spawn_agents", "notes_read"} <= view["tools"]

    # Each helper's work is an A2A task (form delegate, spawned) in the ledger and on the timeline.
    tasks = _of(events, "a2a.task")
    task_ids = {j["task_id"] for j in joined}
    assert {t["task_id"] for t in tasks} == task_ids
    assert sorted(t["state"] for t in tasks) == ["completed", "completed", "working", "working"]
    msgs = [d["message"] for d in _of(events, "a2a.message") if d["message"]["metadata"].get("spawned")]
    assert len(msgs) == 4 and {m["metadata"]["form"] for m in msgs} == {"delegate"}
    assert {m["taskId"] for m in msgs} == task_ids
    async with db.sessionmaker() as s:
        rows = list((await s.execute(select(LedgerEntry).where(LedgerEntry.run_id == ctx.run_id))).scalars())
    assert sum(1 for r in rows if r.protocol == "a2a" and r.detail.get("form") == "delegate") == 4
    assert all(orch.council.tasks[t]["status"]["state"] == "completed" for t in task_ids)

    # Results on the board; then they left: gone from every roster.
    assert sum(1 for n in ctx.notes if n["title"].startswith("Helper result:")) == 2
    left = _of(events, "member.left")
    assert sorted(x["member_id"] for x in left) == sorted(ids) and {x["status"] for x in left} == {
        "completed"
    }
    assert [m.id for m in orch.members] == ["m1", "m2", "m3"] and set(orch.council.members) == {
        "m1",
        "m2",
        "m3",
    }
    # The live view: helpers set it while working and cleared it when done.
    live = _of(events, "member.live")
    assert any(x["member_id"] == ids[0] and x["task"] for x in live)
    assert not set(ids) & set(orch.council.live)


async def test_the_spawn_limits_hold(db, run_row):
    entries = [HelperIn(role="researcher", instructions=f"Task {i}") for i in range(3)]
    orch, *_ = _council(db, run_row, rails={**QUIET, "spawn": {"max_agents": 2}})
    ada = orch.members[0]
    started, refused = await orch.helpers.spawn(ada, entries, None)
    await orch.helpers.wait(started)
    assert len(started) == 2 and len(refused) == 1 and "spawn.max_agents" in refused[0]

    orch, *_ = _council(db, run_row, rails={**QUIET, "spawn": {"max_live": 1}})
    started, refused = await orch.helpers.spawn(orch.members[0], entries[:2], None)
    await orch.helpers.wait(started)
    assert len(started) == 1 and "spawn.max_live" in refused[0]

    orch, *_ = _council(db, run_row, rails={**QUIET, "spawn": {"max_depth": 0}})
    started, refused = await orch.helpers.spawn(orch.members[0], entries[:1], None)
    assert not started and "spawn.max_depth" in refused[0]

    orch, *_ = _council(db, run_row, rails={**QUIET, "spawn": {"enabled": False}})
    started, refused = await orch.helpers.spawn(orch.members[0], entries[:1], None)
    assert not started and "spawn.enabled" in refused[0]


async def test_helpers_spawn_helpers_only_as_deep_as_allowed(db, run_row):
    orch, _ctx, _events, _ = _council(db, run_row, rails={**QUIET, "spawn": {"max_depth": 1}})
    ada = orch.members[0]
    (first,), _ = await orch.helpers.spawn(ada, [HelperIn(role="researcher", instructions="One")], None)
    _, refused = await orch.helpers.spawn(first.spec, [HelperIn(role="analyst", instructions="Two")], None)
    await orch.helpers.wait([first])
    assert "spawn.max_depth" in refused[0] and first.ok


async def test_a_waiting_member_gives_its_parallelism_slot_to_its_helpers(db, run_row):
    orch, _ctx, _events, llm = _council(db, run_row, rails={**QUIET, "max_parallel_agents": 1})
    orch.steps = [Step("s1", "Gather", "Compare the cells", orch.members[0])]
    await asyncio.wait_for(orch.execute(), 10)  # one slot, a parent waiting on two helpers: no deadlock
    assert orch.steps[0].status == "done" and "result from" in orch.steps[0].output
    spans = sorted(s for v in llm.spans.values() for s in v)
    assert len(spans) == 2 and spans[0][1] <= spans[1][0]  # one slot: the helpers took turns


async def test_cancelling_the_run_cancels_its_helpers(db, run_row):
    gate = asyncio.Event()

    def script(req):
        if req.metadata.get("phase") == "spawn" and not any(m.role == "tool" for m in req.messages):
            return reply("looking", [ToolCall(id="n1", name="notes_read", arguments={})])
        return _script(req)

    orch, ctx, events, llm = _council(db, run_row, script, gate=gate)
    orch.steps = [Step("s1", "Gather", "Compare the cells", orch.members[0])]
    work = asyncio.create_task(orch.execute())
    for _ in range(200):
        if llm.waiting == 2:
            break
        await asyncio.sleep(0.02)
    assert llm.waiting == 2 and len(orch.members) == 5  # both helpers are in the roster, working
    ctx.controls.cancelled = True
    gate.set()
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(work, 10)
    assert {x["status"] for x in _of(events, "member.left")} == {"cancelled"}
    assert len(_of(events, "member.left")) == 2 and len(orch.members) == 3
    assert not orch.helpers.running_tasks()
    states = [t["state"] for t in _of(events, "a2a.task")]
    assert states.count("canceled") == 2


async def test_a_resumed_run_replays_its_helpers_work(db, run_row):
    orch, _, events, _ = _council(db, run_row)
    await orch.runner(orch.members[0]).run_task("Compare the cells", step_title="Gather")
    helper_tasks = [e.data for e in events if e.type == TASK_EVENT and e.data["phase"] == "spawn"]
    assert len(helper_tasks) == 2 and all(t["ok"] for t in helper_tasks)
    resume = ResumeState(from_run_id=uuid.uuid4())
    for t in helper_tasks:  # Ada's own task did not finish: only her helpers' work is replayed
        resume.answers[t["key"]].append(f"old {t['member_id']}")

    orch2, _, events2, llm2 = _council(db, run_row)
    orch2.ctx.extra["resume"] = resume
    out = await orch2.runner(orch2.members[0]).run_task("Compare the cells", step_title="Gather")
    assert not [r for r in llm2.requests if r.metadata.get("phase") == "spawn"]  # no helper model call
    replayed = [e.data for e in events2 if e.type == TASK_EVENT and e.data.get("replayed")]
    assert {r["key"] for r in replayed} == {t["key"] for t in helper_tasks}
    assert all(f"old {t['member_id']}" in out.text for t in helper_tasks)


async def test_the_chair_adds_a_step_that_starts_while_the_council_works(db, run_row):
    started = asyncio.Event()
    add = ToolCall(
        id="as1",
        name="add_step",
        arguments={"title": "Check the sources", "instructions": "Check every source", "assignee": "analyst"},
    )

    async def script(req):
        phase, member = req.metadata.get("phase"), req.metadata.get("member")
        done = [m for m in req.messages if m.role == "tool"]
        if phase == "execute" and member == "Cy":
            if not done:
                return reply("one more step is needed", [add])
            await asyncio.wait_for(started.wait(), 5)  # the new step runs while this one still works
            return reply(f"coordinated: {done[-1].content}")
        if phase == "execute" and member == "Ben":
            started.set()
            return reply("sources checked")
        return reply("ok")

    orch, ctx, events, _ = _council(db, run_row, script)
    cy = orch.members[2]
    orch.steps = [Step("s1", "Coordinate", "Coordinate the work", cy)]
    await asyncio.wait_for(orch.execute(), 10)
    assert [s.id for s in orch.steps] == ["s1", "a1"] and all(s.status == "done" for s in orch.steps)
    added = _of(events, "step.added")[0]
    assert added["by"] == "m3" and added["step"]["assignee"] == "m2" and added["step"]["id"] == "a1"
    assert _of(events, "run.plan")[-1]["diff"] == {"added": ["a1"]}
    assert "Added step a1" in orch.steps[0].output

    # Only the chair adds steps, only to known steps, and only while the council works.
    ada = orch.members[0]
    with pytest.raises(ValueError, match="only while"):
        await orch.add_step("Late", "Too late", None, [], by=cy)
    orch._executing = True
    with pytest.raises(ValueError, match="No step called"):
        await orch.add_step("New", "New work", None, ["zz"], by=cy)
    step, added_again = await orch.add_step("Check the sources", "Check every source", None, [], by=cy)
    assert step.id == "a1" and not added_again
    tool = orch.tools.get("add_step")
    from argus.tools.base import ToolCallContext

    refused = await tool.invoke(
        {"title": "x", "instructions": "y"}, ToolCallContext(ctx, ada.id, "Ada", "r", None, "c")
    )
    assert refused.is_error and "Only the chair" in refused.content


async def test_a_resumed_run_restores_the_steps_its_old_run_added(db, run_row):
    orch, ctx, _events, _ = _council(db, run_row, lambda req: reply("done"))
    resume = ResumeState(from_run_id=uuid.uuid4())
    resume.added_steps = [
        {
            "id": "a1",
            "title": "Check",
            "instructions": "Check it",
            "assignee": "m2",
            "depends_on": [],
            "by": "m3",
        }
    ]
    ctx.extra["resume"] = resume
    orch.steps = [Step("s1", "Gather", "Gather", orch.members[0])]
    await asyncio.wait_for(orch.execute(), 10)
    assert [(s.id, s.assignee.id, s.status) for s in orch.steps] == [
        ("s1", "m1", "done"),
        ("a1", "m2", "done"),
    ]


async def test_an_offline_comparison_spawns_two_helpers_and_the_user_can_correct_one(client, auth, app):
    gate = asyncio.Event()
    waiting: list[str] = []

    async def gated(self, request):
        self.calls.append(request)
        meta = request.metadata
        if meta.get("phase") == "spawn" and not any(m.role == "tool" for m in request.messages):
            waiting.append(meta["member_id"])
            await gate.wait()
            out = {"content": "reading the board", "tool_calls": [{"name": "notes_read", "arguments": {}}]}
            return MockProvider._build(self, request, out)
        return MockProvider._build(self, request, council_responder(request))

    class Gated(MockProvider):
        complete = gated

    app.state.llm.system["gated-spawn"] = Gated("gated-spawn")
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Solid-state vs lithium-ion batteries",
            "mode": "research",
            "provider": "gated-spawn",
            "model": "m",
            "guardrails": {"oversight": {"monitor_sensitivity": "off"}},
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    await _wait_for_event(client, auth, rid, "member.joined")
    for _ in range(200):  # both helpers reach their first turn at the same time
        if len(waiting) == 2:
            break
        await asyncio.sleep(0.02)
    assert len(waiting) == 2
    helper = waiting[0]
    res = await client.post(
        f"{API}/{rid}/interventions",
        headers=auth,
        json={"kind": "hold", "target": {"member_id": helper}, "text": "Cite primary sources only"},
    )
    assert res.status_code == 202, res.text
    gate.set()
    run = await wait_status(client, auth, rid, timeout=30)
    assert run["status"] == "completed", run

    provider = app.state.llm.system["gated-spawn"]
    later = [
        c
        for c in provider.calls
        if c.metadata.get("member_id") == helper and any(m.role == "tool" for m in c.messages)
    ]
    assert later and any(
        "[Correction from user] Cite primary sources only" in m.content for m in later[0].messages
    )
    evs = await events_of(client, auth, rid)
    joined = [e["data"] for e in evs if e["type"] == "member.joined"]
    assert len(joined) == 2 and {j["member"]["role"] for j in joined} == {"researcher"}
    assert {e["data"]["status"] for e in evs if e["type"] == "member.left"} == {"completed"}
    spawned = [
        e["data"]["message"]
        for e in evs
        if e["type"] == "a2a.message" and e["data"]["message"]["metadata"].get("spawned")
    ]
    assert len(spawned) == 4
    # Their member ids are gone once they left: a hold on one is refused.
    late = await client.post(
        f"{API}/{rid}/interventions", headers=auth, json={"kind": "hold", "target": {"member_id": helper}}
    )
    assert late.status_code in (404, 409)
