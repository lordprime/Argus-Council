"""Resuming a halted run: finished tasks are replayed, only unfinished work calls a model."""

import uuid

from argus.providers.mock import council_responder
from argus.providers.types import CompletionResponse, Usage
from argus.runtime.resume import ResumeState, _recover_legacy, task_key
from tests.test_runs import API, events_of, install, types, wait_status


def _counting(name: str, calls: list[int], cost: float, free: int = 0):
    """A council model that counts its calls; the first ``free`` calls cost nothing."""

    def responder(req):
        calls.append(1)
        out = council_responder(req)
        return CompletionResponse(
            provider=name,
            model="m",
            content=out["content"],
            tool_calls=out.get("tool_calls", []),
            usage=Usage(input_tokens=10, output_tokens=10, cost_usd=0.0 if len(calls) <= free else cost),
        )

    return responder


async def _start(client, auth, provider: str, usd: float) -> str:
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Grid storage economics",
            "mode": "research",
            "provider": provider,
            "model": "m",
            # Each run starts on a fresh board: these tests count the calls a run makes.
            "guardrails": {"budget": {"usd": usd}, "graph": {"inherit_board": False}},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_a_halted_run_resumes_where_it_stopped(client, auth, app):
    full: list[int] = []
    install(app, "fullrun", _counting("fullrun", full, 0.01))
    done = await wait_status(client, auth, await _start(client, auth, "fullrun", 50))
    assert done["status"] == "completed"

    # Free until well into the report, then one call blows the budget: the run halts late.
    first: list[int] = []
    install(app, "pricey", _counting("pricey", first, 100.0, free=len(full) - 12))
    halted = await wait_status(client, auth, await _start(client, auth, "pricey", 5.0))
    assert halted["status"] == "halted" and "budget" in halted["error"].lower()
    old_events = await events_of(client, auth, halted["id"])
    finished = [e for e in old_events if e["type"] == "task.completed" and e["data"]["ok"]]
    assert finished, "the halted run finished some tasks"

    # Resume with a higher budget: the old run's finished tasks are replayed, not asked again.
    resumed_calls: list[int] = []
    install(app, "pricey", _counting("pricey", resumed_calls, 0.01))
    r = await client.post(
        f"{API}/{halted['id']}/continue", headers=auth, json={"guardrails": {"budget": {"usd": 50}}}
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["resumed_from_id"] == halted["id"] and new["topic"] == halted["topic"]
    run = await wait_status(client, auth, new["id"])
    assert run["status"] == "completed", run
    evs = await events_of(client, auth, run["id"])
    resumed = next(e for e in evs if e["type"] == "run.resumed")["data"]
    assert (
        resumed["from_run_id"] == halted["id"] and resumed["tasks"] == len(finished) and not resumed["legacy"]
    )
    replayed = [e for e in evs if e["type"] == "task.completed" and e["data"].get("replayed")]
    # Every finished task is replayed, except a teammate's answer to an A2A ask or review made from
    # inside a step that is itself replayed: that step never asks again, so its answer is not needed.
    own = [e for e in finished if not e["data"]["phase"].startswith("a2a_")]
    assert len(own) <= len(replayed) <= len(finished)
    assert {e["data"]["key"] for e in own} <= {e["data"]["key"] for e in replayed}
    # The plan is the old run's, and the model was asked only for what never finished.
    old_plan = next(e for e in old_events if e["type"] == "run.plan")["data"]["steps"]
    new_plan = next(e for e in evs if e["type"] == "run.plan")["data"]["steps"]
    assert [s["title"] for s in new_plan] == [s["title"] for s in old_plan]
    assert len(resumed_calls) < len(full)
    assert len(first) + len(resumed_calls) <= len(full) + 1  # at most the call cut off by the halt
    # Citations carried over keep their numbers.
    old_sources = [e["data"]["url"] for e in old_events if e["type"] == "citation.added"]
    new_sources = [e["data"]["url"] for e in evs if e["type"] == "citation.added"]
    assert new_sources[: len(old_sources)] == old_sources
    assert "run.plan" in types(evs)


async def test_only_stopped_runs_of_your_own_can_be_resumed(client, auth, app):
    from tests.conftest import register_and_login

    install(app, "cheap", _counting("cheap", [], 0.0))
    done = await wait_status(client, auth, await _start(client, auth, "cheap", 50))
    assert done["status"] == "completed"
    assert (await client.post(f"{API}/{done['id']}/continue", headers=auth)).status_code == 409
    other = await register_and_login(client, "mallory@example.com")
    assert (await client.post(f"{API}/{done['id']}/continue", headers=other)).status_code == 404
    assert (await client.post(f"{API}/{uuid.uuid4()}/continue", headers=auth)).status_code == 404


def test_runs_from_before_task_records_recover_the_plan_and_finished_steps():
    plan_text = '{"steps": [{"id": "s1", "title": "Gather"}, {"id": "s2", "title": "Analyse"}]}'
    chair = {"kind": "agent", "member_id": "m-chair"}
    events = [
        {
            "type": "thought.completed",
            "seq": 1,
            "actor": chair,
            "data": {"text": "thinking", "tool_calls": [{"id": "t"}]},
        },
        {
            "type": "thought.completed",
            "seq": 2,
            "actor": chair,
            "data": {"text": plan_text, "tool_calls": []},
        },
        {
            "type": "run.plan",
            "seq": 3,
            "data": {
                "steps": [
                    {"id": "s1", "title": "Gather", "assignee_member_id": "m-r"},
                    {"id": "s2", "title": "Analyse", "assignee_member_id": "m-a"},
                ]
            },
        },
        {"type": "step.status", "seq": 4, "data": {"step_id": "s1", "status": "running"}},
        {
            "type": "thought.completed",
            "seq": 5,
            "step_id": "s1",
            "data": {"text": "draft", "tool_calls": [{"id": "x"}]},
        },
        {
            "type": "thought.completed",
            "seq": 6,
            "step_id": "s1",
            "data": {"text": "findings", "tool_calls": []},
        },
        {"type": "step.status", "seq": 7, "data": {"step_id": "s1", "status": "done"}},
        {"type": "step.status", "seq": 8, "data": {"step_id": "s2", "status": "running"}},
        {"type": "thought.completed", "seq": 9, "step_id": "s2", "data": {"text": "half", "tool_calls": []}},
    ]
    state = ResumeState(from_run_id=uuid.uuid4())
    _recover_legacy(state, events)
    assert state.legacy
    assert state.take(task_key("plan", None, "Planning", "m-chair")) == plan_text
    assert state.take(task_key("execute", "s1", "Gather", "m-r")) == "findings"
    # s2 never finished: it runs again.
    assert state.take(task_key("execute", "s2", "Analyse", "m-a")) is None
    assert state.replayed == 2


async def test_a_run_with_linked_knowledge_resumes_with_it(client, auth, app):
    # Linked knowledge base ids are stored as strings; resuming re-checks them against the owner's
    # knowledge bases, which once compared strings with UUIDs and refused every linked run.
    kb = (await client.post("/api/v1/knowledge-bases", headers=auth, json={"name": "prices"})).json()
    install(app, "pricey", _counting("pricey", [], 0.4, free=2))
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Grid storage economics",
            "mode": "research",
            "provider": "pricey",
            "model": "m",
            "knowledge_base_ids": [kb["id"]],
            "guardrails": {"budget": {"usd": 1}},
        },
    )
    assert r.status_code == 201, r.text
    old = await wait_status(client, auth, r.json()["id"])
    assert old["status"] == "halted", old["status"]
    resumed = await client.post(f"{API}/{old['id']}/continue", headers=auth)
    assert resumed.status_code == 201, resumed.text
    linked = (await client.get(f"{API}/{resumed.json()['id']}/knowledge", headers=auth)).json()["linked"]
    assert [k["id"] for k in linked] == [kb["id"]]
