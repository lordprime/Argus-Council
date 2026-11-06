"""Auto mode: approvals and limit raises answered without asking, on the record, switchable live."""

import asyncio

import pytest

from argus.providers.types import Usage
from argus.runtime.events import Actor
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Guardrails
from tests import test_limit_extension as limits
from tests.test_limit_extension import QUIET, FakeLLM, make_ctx, reply, types
from tests.test_runs import (
    API,
    RiskyTool,
    _tool_council,
    _tool_run_setup,
    _wait_for_event,
    events_of,
    wait_status,
)

db, run_row = limits.db, limits.run_row  # the same fixtures


async def _pending(ctx):
    for _ in range(500):
        if ctx.approvals:
            return next(iter(ctx.approvals.values()))
        await asyncio.sleep(0.01)
    raise AssertionError("no approval was requested")


# ------------------------------------------------------------------ the run context
async def test_auto_mode_approves_a_tool_request_at_once_and_records_it(db, run_row):
    ctx, events = make_ctx(db, run_row, {**QUIET, "auto_mode": True}, FakeLLM(lambda r: reply()))
    decision = await ctx.request_approval(Actor(kind="agent", member_id="m1"), "run shell", ["shell"])
    assert decision == {"decision": "approved", "by": "auto"}
    assert ctx.approvals == {}
    req = next(e for e in events if e.type == "approval.requested")
    res = next(e for e in events if e.type == "approval.resolved")
    assert req.data["auto"] is True and res.data["approval_id"] == req.data["approval_id"]
    assert res.data["by"] == "auto" and res.actor.kind == "system"


async def test_auto_mode_alone_still_asks_before_raising_a_limit(db, run_row):
    ctx, events = make_ctx(
        db, run_row, {**QUIET, "auto_mode": True, "max_steps": 4}, FakeLLM(lambda r: reply())
    )
    ctx.engine.steps = 5
    task = asyncio.create_task(ctx.checkpoint())
    approval = await _pending(ctx)
    assert approval.risk == ["limit_extension"] and not task.done()
    # Switching limit raises on answers the open request.
    assert ctx.set_auto_mode(extensions=True) == {"tools": True, "extensions": True}
    await asyncio.wait_for(task, 2)
    assert ctx.engine.rails.max_steps == 6
    ext = next(e for e in events if e.type == "guardrail.extended")
    assert ext.data["auto"] is True and ext.actor.kind == "system"


async def test_auto_limit_raises_stop_at_the_cap_and_only_for_listed_limits(db, run_row):
    rails = {**QUIET, "max_steps": 2, "extensions": {"auto_approve": True, "max_extensions": 1}}
    ctx, events = make_ctx(db, run_row, rails, FakeLLM(lambda r: reply()))
    ctx.engine.steps = 3
    await ctx.checkpoint()  # 2 -> 3, without anyone answering
    assert ctx.engine.rails.max_steps == 3
    ctx.engine.steps = 4
    with pytest.raises(GuardrailBreach):
        await ctx.checkpoint()  # the one allowed extension is used up
    assert types(events).count("approval.requested") == 1
    # Money is not in the default list: an auto raise never touches it.
    ctx.engine.record_usage(Usage(cost_usd=99))
    ctx.engine.steps = 0
    with pytest.raises(GuardrailBreach) as err:
        await ctx.checkpoint()
    assert err.value.violation.rule == "budget.usd"
    assert types(events).count("approval.requested") == 1


def test_auto_mode_leaves_what_the_guardrails_block_blocked():
    rails = Guardrails.resolve({"auto_mode": True, "tools": {"deny": ["deploy"]}})
    engine = GuardrailEngine(rails)
    assert engine.tool_allowed("deploy") is not None
    assert engine.check_tool_args("shell", {"command": "rm -rf /"}) is not None


def test_sub_councils_inherit_auto_mode_from_the_parent():
    on = Guardrails.resolve({"auto_mode": True, "extensions": {"auto_approve": True}})
    child = Guardrails.resolve({}).clamp_to(on)
    assert child.auto_mode is True and child.extensions.enabled is False
    # A child never answers on its own when the parent asks the user.
    off = Guardrails.resolve({})
    assert Guardrails.resolve({"auto_mode": True}).clamp_to(off).auto_mode is False


# ------------------------------------------------------------------ the API
async def test_a_run_in_auto_mode_never_waits_for_the_user(client, auth, app):
    _tool_run_setup(app, "autoy")
    cid = await _tool_council(
        client, auth, "autoy", {"oversight": {"monitor_sensitivity": "off"}, "auto_mode": True}
    )
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed" and RiskyTool.calls == ["staging"]
    evs = await events_of(client, auth, rid)
    resolved = next(e for e in evs if e["type"] == "approval.resolved")
    assert resolved["data"] == {
        "approval_id": resolved["data"]["approval_id"],
        "decision": "approved",
        "by": "auto",
    }
    snap = (await client.get(f"{API}/{rid}/snapshot", headers=auth)).json()
    assert snap["open_approvals"] == []
    # Only the finished run reaches the bell: nothing was waiting for the user.
    for _ in range(100):
        kinds = [
            n["kind"]
            for n in (await client.get("/api/v1/notifications", headers=auth)).json()["notifications"]
        ]
        if "run.completed" in kinds:
            break
        await asyncio.sleep(0.05)
    assert "run.completed" in kinds and "approval.requested" not in kinds


async def test_switching_auto_mode_on_live_answers_the_open_request(client, auth, app):
    _tool_run_setup(app, "autolive")
    cid = await _tool_council(client, auth, "autolive", {"oversight": {"monitor_sensitivity": "off"}})
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    await _wait_for_event(client, auth, rid, "approval.requested")
    assert RiskyTool.calls == []

    from tests.conftest import register_and_login

    other = await register_and_login(client, "mallory@example.com")
    assert (
        await client.post(f"{API}/{rid}/auto-mode", headers=other, json={"tools": True})
    ).status_code == 404

    r = await client.post(f"{API}/{rid}/auto-mode", headers=auth, json={"tools": True})
    assert r.status_code == 202 and r.json() == {"tools": True, "extensions": False}
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed" and RiskyTool.calls == ["staging"]
    evs = await events_of(client, auth, rid)
    flags = next(e for e in evs if e["type"] == "run.auto_mode")
    assert flags["data"] == {"tools": True, "extensions": False} and flags["actor"]["kind"] == "user"
    assert next(e for e in evs if e["type"] == "approval.resolved")["data"]["by"] == "auto"
    snap = (await client.get(f"{API}/{rid}/snapshot", headers=auth)).json()
    assert snap["auto_mode"] == {"tools": True, "extensions": False}
    assert next(iter(snap["approvals"].values()))["by"] == "auto"
    # The run is over: like pause and resume, the switch is refused.
    assert (
        await client.post(f"{API}/{rid}/auto-mode", headers=auth, json={"tools": False})
    ).status_code == 409
