"""The budget reserve: drawn instead of halting, counted by the report, adjustable on a live run."""

from argus.providers.mock import council_responder
from argus.providers.types import CompletionResponse, Usage
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from tests.test_runs import (
    API,
    RiskyTool,
    _tool_council,
    _tool_run_setup,
    _wait_for_event,
    events_of,
    install,
    wait_status,
)


def _priced(name: str, cost: float):
    def responder(req):
        out = council_responder(req)
        return CompletionResponse(
            provider=name,
            model="m",
            content=out["content"],
            tool_calls=out.get("tool_calls", []),
            usage=Usage(input_tokens=10, output_tokens=10, cost_usd=cost),
        )

    return responder


def test_the_reserve_is_drawn_whole_and_once():
    engine = GuardrailEngine(Guardrails.resolve({"budget": {"usd": 1.0, "buffer_usd": 4.0}}))
    assert engine.buffer("budget.usd") == 4.0 and engine.buffer("max_steps") == 0
    assert engine.draw_buffer("budget.usd") == (1.0, 5.0)
    assert engine.limit("budget.usd") == 5.0 and engine.buffer("budget.usd") == 0
    assert engine.draw_buffer("budget.usd") is None
    assert engine.budget_snapshot()["buffer"] == {"usd": 0, "tokens": 0}


async def test_a_run_draws_its_reserve_instead_of_halting(client, auth, app):
    install(app, "spendy", _priced("spendy", 0.4))
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "t",
            "mode": "research",
            "provider": "spendy",
            "model": "m",
            # Raising limits is switched off: only the reserve can keep the run going.
            "guardrails": {"budget": {"usd": 1.0, "buffer_usd": 100.0}, "extensions": {"enabled": False}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    drawn = [e for e in await events_of(client, auth, run["id"]) if e["type"] == "guardrail.extended"]
    assert len(drawn) == 1 and drawn[0]["data"]["buffer"] is True
    assert drawn[0]["data"]["from"] == 1.0 and drawn[0]["data"]["to"] == 101.0
    assert drawn[0]["actor"]["kind"] == "system"


async def test_the_budget_of_a_live_run_can_be_raised_and_its_reserve_set(client, auth, app):
    _tool_run_setup(app, "budgetlive")
    cid = await _tool_council(client, auth, "budgetlive", {"oversight": {"monitor_sensitivity": "off"}})
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    await _wait_for_event(client, auth, rid, "approval.requested")  # the run waits, live
    r = await client.post(f"{API}/{rid}/budget", headers=auth, json={"add_usd": 2.5, "buffer_usd": 3})
    assert r.status_code == 202, r.text
    snap = r.json()
    assert snap["limits"]["usd"] == 7.5 and snap["buffer"]["usd"] == 3
    assert (await client.post(f"{API}/{rid}/budget", headers=auth, json={"add_usd": -1})).status_code == 422
    evs = await events_of(client, auth, rid)
    adjusted = next(e for e in evs if e["type"] == "budget.adjusted")
    assert adjusted["data"]["budget.usd"] == {"from": 5.0, "to": 7.5} and adjusted["actor"]["kind"] == "user"
    await client.post(f"{API}/{rid}/cancel", headers=auth)
    await wait_status(client, auth, rid)
    assert RiskyTool.calls == []
    # A finished run has no budget to change.
    assert (await client.post(f"{API}/{rid}/budget", headers=auth, json={"add_usd": 1})).status_code == 409
