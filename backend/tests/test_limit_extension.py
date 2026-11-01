"""Raising a run's limits with the user's approval, and condensing an agent's history."""

import asyncio
import uuid

import pytest
from pydantic import BaseModel

import argus.runtime.history as history
from argus.db.models import Run, User
from argus.db.session import Database
from argus.providers.types import CompletionResponse, Message, StreamEvent, ToolCall, Usage
from argus.runtime.agent import AgentRunner, MemberSpec
from argus.runtime.context import RunContext
from argus.runtime.events import EventSink, MemoryBroker
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Guardrails
from argus.runtime.history import CONDENSED_HEADER, condense_split
from argus.runtime.oversight import ThoughtBus
from argus.tools.base import Tool, ToolCallContext, ToolRegistry, ToolResult


@pytest.fixture
async def db(settings):
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def run_row(db):
    async with db.sessionmaker() as s:
        u = User(email=f"{uuid.uuid4().hex}@x.io", password_hash="x")
        s.add(u)
        await s.flush()
        r = Run(owner_id=u.id, topic="t")
        s.add(r)
        await s.commit()
        return r


class FakeLLM:
    """Answers each request with ``script(request)``; records every request."""

    def __init__(self, script):
        self.script = script
        self.requests = []

    def _respond(self, req):
        self.requests.append(req)
        out = self.script(req)
        if isinstance(out, Exception):
            raise out
        return out

    async def complete(self, req, route, **kw):
        return self._respond(req)

    async def stream(self, req, route, **kw):
        yield StreamEvent(type="done", response=self._respond(req))


def reply(content="", calls=None, tokens=10):
    return CompletionResponse(
        provider="fake",
        model="m",
        content=content,
        tool_calls=calls or [],
        usage=Usage(input_tokens=tokens, output_tokens=tokens),
    )


class Echo(Tool):
    name = "echo"
    description = "Echo"

    class Args(BaseModel):
        n: int
        pad: int = 0

    async def run(self, args, ctx: ToolCallContext) -> ToolResult:
        return ToolResult(f"echo {args.n} " + "x" * args.pad)


def make_ctx(db, run_row, rails, llm, depth=0, parent=None):
    sink = EventSink(run_row.id, None, db.sessionmaker, MemoryBroker())
    events = []
    sink.listeners.append(events.append)
    ctx = RunContext(
        run_id=run_row.id,
        owner_id=run_row.owner_id,
        topic="t",
        description="",
        mode="research",
        outputs=["markdown"],
        sink=sink,
        engine=GuardrailEngine(Guardrails.resolve(rails), parent=parent),
        llm=llm,
        sessionmaker=db.sessionmaker,
        approval_timeout_s=5,
        depth=depth,
    )
    return ctx, events


def answer_approvals(ctx, decision="approved", edited=None, log=None):
    """Background task resolving every approval as it appears."""

    async def loop():
        while True:
            for aid, a in list(ctx.approvals.items()):
                if not a.future.done():
                    if log is not None:
                        log.append(a.summary)
                    payload = {"decision": decision}
                    if edited is not None:
                        payload["edited_args"] = edited
                    ctx.resolve_approval(aid, payload)
            await asyncio.sleep(0.01)

    return asyncio.create_task(loop())


QUIET = {"oversight": {"monitor_sensitivity": "off"}}


def types(events):
    return [e.type for e in events]


# ------------------------------------------------------------------ run limits
async def test_approved_extension_raises_max_steps_and_the_run_goes_on(db, run_row):
    ctx, events = make_ctx(db, run_row, {**QUIET, "max_steps": 4}, FakeLLM(lambda r: reply()))
    asked = []
    task = answer_approvals(ctx, log=asked)
    try:
        ctx.engine.steps = 5
        await ctx.checkpoint()
    finally:
        task.cancel()
    assert ctx.engine.rails.max_steps == 6  # 4 + 50% of 4
    assert "step limit (4 steps)" in asked[0] and "Raise it to 6" in asked[0]
    ext = next(e for e in events if e.type == "guardrail.extended")
    assert ext.data["rule"] == "max_steps" and (ext.data["from"], ext.data["to"]) == (4, 6)
    assert "approval.requested" in types(events) and not ext.ephemeral


async def test_rejected_extension_halts_and_is_not_asked_again(db, run_row):
    ctx, events = make_ctx(db, run_row, {**QUIET, "max_steps": 4}, FakeLLM(lambda r: reply()))
    task = answer_approvals(ctx, decision="rejected")
    try:
        ctx.engine.steps = 5
        with pytest.raises(GuardrailBreach) as err:
            await ctx.checkpoint()
        assert err.value.violation.rule == "max_steps"
        with pytest.raises(GuardrailBreach):
            await ctx.checkpoint()
    finally:
        task.cancel()
    assert types(events).count("approval.requested") == 1
    assert ctx.engine.rails.max_steps == 4


async def test_the_approver_can_choose_the_new_limit(db, run_row):
    ctx, _ = make_ctx(db, run_row, {**QUIET, "budget": {"tokens": 100}}, FakeLLM(lambda r: reply()))
    task = answer_approvals(ctx, edited={"limit": 1000})
    try:
        ctx.engine.record_usage(Usage(input_tokens=150))
        await ctx.checkpoint()
    finally:
        task.cancel()
    assert ctx.engine.rails.budget.tokens == 1000


async def test_extensions_stop_at_the_cap_and_usd_is_opt_in(db, run_row):
    rails = {**QUIET, "max_steps": 2, "extensions": {"max_extensions": 1}}
    ctx, _ = make_ctx(db, run_row, rails, FakeLLM(lambda r: reply()))
    task = answer_approvals(ctx)
    try:
        ctx.engine.steps = 3
        await ctx.checkpoint()  # 2 -> 3
        ctx.engine.steps = 4
        with pytest.raises(GuardrailBreach):
            await ctx.checkpoint()
        ctx.engine.record_usage(Usage(cost_usd=99))
        assert not ctx.engine.can_extend("budget.usd")
    finally:
        task.cancel()


async def test_agents_hitting_the_limit_together_share_one_request(db, run_row):
    ctx, events = make_ctx(db, run_row, {**QUIET, "max_steps": 10}, FakeLLM(lambda r: reply()))
    ctx.engine.steps = 11
    task = answer_approvals(ctx)
    try:
        await asyncio.gather(*(ctx.checkpoint(f"m{i}") for i in range(4)))
    finally:
        task.cancel()
    assert types(events).count("approval.requested") == 1
    assert ctx.engine.rails.max_steps == 15


async def test_waiting_for_the_approval_does_not_use_up_the_runtime(db, run_row):
    now = [0.0]
    ctx, _ = make_ctx(db, run_row, {**QUIET, "max_runtime_minutes": 1}, FakeLLM(lambda r: reply()))
    ctx.engine.clock = lambda: now[0]
    ctx.engine.started = 0.0
    now[0] = 61.0  # over the limit: the request must still wait for the user, not expire at once

    async def approve_later():
        for _ in range(500):
            if ctx.approvals:
                break
            await asyncio.sleep(0.01)
        now[0] = 61.0 + 600  # ten minutes pass while the user decides
        for aid in list(ctx.approvals):
            ctx.resolve_approval(aid, {"decision": "approved"})

    task = asyncio.create_task(approve_later())
    await ctx.checkpoint()
    await task
    assert ctx.engine.rails.max_runtime_minutes == 2  # at least a minute per extension
    assert ctx.engine.runtime_s == pytest.approx(61.0)  # the wait is not counted


async def test_sub_councils_never_ask(db, run_row):
    parent = GuardrailEngine(Guardrails.resolve(QUIET))
    child_rails = Guardrails.resolve({**QUIET, "max_steps": 2}).clamp_to(parent.rails)
    assert child_rails.extensions.enabled is False
    ctx, events = make_ctx(db, run_row, {**QUIET, "max_steps": 2}, FakeLLM(lambda r: reply()), parent=parent)
    ctx.engine.steps = 3
    with pytest.raises(GuardrailBreach):
        await ctx.checkpoint()
    assert "approval.requested" not in types(events)


# ------------------------------------------------------------------ agent turns
def agent(ctx, tools=None):
    member = MemberSpec(
        id="m1", name="Ada", role="researcher", provider="fake", model="m", system_prompt="", tools=["*"]
    )
    return AgentRunner(member, ctx, ToolRegistry(tools or [Echo()]), ThoughtBus(ctx, []), "brief")


def tool_loop(until):
    """Call echo (with a fresh argument each time) until ``until`` turns, then answer."""
    turns = [0]

    def script(req):
        if req.metadata.get("phase") == "condense":
            return reply("SUMMARY: echoed 1..n")
        turns[0] += 1
        if turns[0] > until:
            return reply("done")
        return reply("", [ToolCall(id=f"c{turns[0]}", name="echo", arguments={"n": turns[0]})])

    return script


async def test_agent_asks_for_more_turns_when_still_working(db, run_row):
    rails = {**QUIET, "max_agent_iterations": 4, "approval_required": []}
    ctx, events = make_ctx(db, run_row, rails, FakeLLM(tool_loop(5)))
    task = answer_approvals(ctx)
    try:
        out = await agent(ctx).run_task("go")
    finally:
        task.cancel()
    assert out.ok and out.text == "done" and out.iterations == 6
    assert ctx.engine.rails.max_agent_iterations == 6
    assert [e.data["rule"] for e in events if e.type == "guardrail.extended"] == ["max_agent_iterations"]


async def test_agent_stops_at_its_turn_limit_when_refused(db, run_row):
    rails = {**QUIET, "max_agent_iterations": 3, "approval_required": []}
    ctx, _ = make_ctx(db, run_row, rails, FakeLLM(tool_loop(10)))
    task = answer_approvals(ctx, decision="rejected")
    try:
        out = await agent(ctx).run_task("go")
    finally:
        task.cancel()
    assert out.reason == "iteration_limit" and out.iterations == 3


def test_condense_split_keeps_tool_calls_with_their_results():
    msgs = [Message.system("s"), Message.user("task")]
    for i in range(5):
        msgs.append(Message.assistant("", [ToolCall(id=f"c{i}", name="echo", arguments={})]))
        msgs.append(Message.tool_result(f"c{i}", "r"))
    cut = condense_split(msgs)
    assert msgs[cut].role == "assistant" and cut == len(msgs) - 4
    assert condense_split(msgs[:6]) is None  # too little to condense


async def test_long_histories_are_condensed_into_the_task_message(db, run_row, monkeypatch):
    rails = {
        **QUIET,
        "max_agent_iterations": 20,
        "approval_required": [],
        "context": {"budget_chars": 20_000},
    }
    llm = FakeLLM(tool_loop(8))
    ctx, events = make_ctx(db, run_row, rails, llm)

    class Big(Echo):
        async def run(self, args, ctx):
            return ToolResult("y" * 6_000)

    runner = agent(ctx, [Big()])
    original = runner._fit_context
    seen = []

    async def spy(messages, task_message, step_id):
        await original(messages, task_message, step_id)
        seen.append([m.model_copy() for m in messages])

    runner._fit_context = spy
    # Keep tool results verbatim (as use_skill's are) so truncation alone cannot fit the budget.
    monkeypatch.setattr(history, "_KEEP_PREFIXES", ('<tool_output tool="echo"',))
    out = await runner.run_task("collect things")
    assert out.ok
    condensed = [e for e in events if e.type == "context.condensed"]
    assert condensed and condensed[0].data["chars_after"] < condensed[0].data["chars_before"]
    after = seen[-1]
    assert CONDENSED_HEADER in after[1].content and "SUMMARY" in after[1].content
    assert after[1].content.startswith("Your task:\ncollect things")
    # Every tool result still follows the assistant turn that called it.
    called = set()
    for m in after:
        called |= {c.id for c in m.tool_calls}
        if m.role == "tool":
            assert m.tool_call_id in called
    assert any(r.metadata.get("phase") == "condense" for r in llm.requests)
    assert ctx.by_member["m1"]["tokens"] > 0


async def test_a_failed_condensation_is_not_fatal(db, run_row, monkeypatch):
    rails = {
        **QUIET,
        "max_agent_iterations": 20,
        "approval_required": [],
        "context": {"budget_chars": 20_000},
    }
    monkeypatch.setattr(history, "_KEEP_PREFIXES", ('<tool_output tool="echo"',))
    inner = tool_loop(8)

    def script(req):
        if req.metadata.get("phase") == "condense":
            return RuntimeError("provider down")
        return inner(req)

    llm = FakeLLM(script)
    ctx, events = make_ctx(db, run_row, rails, llm)

    class Big(Echo):
        async def run(self, args, ctx):
            return ToolResult("y" * 6_000)

    out = await agent(ctx, [Big()]).run_task("collect")
    assert out.ok and out.text == "done"
    assert any(r.metadata.get("phase") == "condense" for r in llm.requests)
    assert "context.condensed" not in types(events)
