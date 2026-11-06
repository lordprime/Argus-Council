"""Tool calls of one turn run at the same time when every one of them is parallel-safe."""

import asyncio
import itertools
import time

from pydantic import BaseModel

from argus.providers.types import ToolCall
from argus.runtime.agent import AgentRunner, MemberSpec
from argus.runtime.oversight import ThoughtBus
from argus.tools.base import Tool, ToolCallContext, ToolRegistry, ToolResult
from argus.tools.builtin import builtin_tools
from argus.tools.council import council_tools
from argus.tools.knowledge import knowledge_tools
from argus.tools.spawn import spawn_tools
from argus.tools.web import WebSearch, web_tools
from tests import test_limit_extension as limits
from tests.test_limit_extension import QUIET, FakeLLM, answer_approvals, make_ctx, reply

db, run_row = limits.db, limits.run_row  # the same fixtures

DELAY = 0.15


class _Timed(Tool):
    """Sleeps, and records when each call started and ended."""

    log: list[tuple[str, int, float, float]] = []

    class Args(BaseModel):
        n: int

    async def run(self, args, ctx: ToolCallContext) -> ToolResult:
        t0 = time.monotonic()
        await asyncio.sleep(DELAY)
        self.log.append((self.name, args.n, t0, time.monotonic()))
        return ToolResult(f"{self.name} {args.n}")


class SlowRead(_Timed):
    name = "slow_read"
    description = "Read something slowly"
    parallel_safe = True


class GatedRead(_Timed):
    name = "gated_read"
    description = "Read something that needs approval in this run"
    parallel_safe = True
    risk = frozenset({"network"})


class SlowWrite(_Timed):
    name = "slow_write"
    description = "Write something slowly"
    risk = frozenset({"fs_write"})


def _runner(db, run_row, calls, rails=None):
    _Timed.log = []
    turns = iter([reply("working", calls), reply("done")])
    llm = FakeLLM(lambda req: next(turns))
    ctx, events = make_ctx(db, run_row, rails or QUIET, llm)
    member = MemberSpec(id="m1", name="Ada", role="researcher", provider="fake", model="m", system_prompt="")
    tools = ToolRegistry([SlowRead(), GatedRead(), SlowWrite()])
    return AgentRunner(member, ctx, tools, ThoughtBus(ctx, []), "brief"), ctx, events, llm


def _call(i, name, n):
    return ToolCall(id=f"c{i}", name=name, arguments={"n": n})


def _overlap(spans):
    return max(s for s, _ in spans) < min(e for _, e in spans)


async def test_parallel_safe_calls_of_one_turn_run_at_the_same_time_and_keep_their_order(db, run_row):
    calls = [_call(1, "slow_read", 3), _call(2, "slow_read", 1), _call(3, "slow_read", 2)]
    runner, _, events, llm = _runner(db, run_row, calls)
    t0 = time.monotonic()
    out = await runner.run_task("Look things up")
    took = time.monotonic() - t0
    assert out.ok and out.tool_calls == 3
    assert _overlap([(s, e) for *_, s, e in _Timed.log])
    assert took < 3 * DELAY  # not one after the other
    # The history keeps the calls' order, whichever finished first.
    results = [m for m in llm.requests[-1].messages if m.role == "tool"]
    assert [r.tool_call_id for r in results] == ["c1", "c2", "c3"]
    assert ["slow_read 3" in results[0].content, "slow_read 1" in results[1].content] == [True, True]
    assert sum(1 for e in events if e.type == "tool.call.completed") == 3


async def test_a_turn_with_a_writing_tool_runs_its_calls_in_order(db, run_row):
    calls = [_call(1, "slow_read", 1), _call(2, "slow_write", 2), _call(3, "slow_read", 3)]
    runner, *_ = _runner(db, run_row, calls)
    await runner.run_task("Read, write, read")
    assert [(name, n) for name, n, *_ in _Timed.log] == [
        ("slow_read", 1),
        ("slow_write", 2),
        ("slow_read", 3),
    ]
    spans = [(s, e) for *_, s, e in _Timed.log]
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in itertools.pairwise(spans))


async def test_a_turn_with_an_approval_gated_call_runs_in_order(db, run_row):
    calls = [_call(1, "gated_read", 1), _call(2, "slow_read", 2)]
    rails = {**QUIET, "approval_required": ["network"]}
    runner, ctx, *_ = _runner(db, run_row, calls, rails)
    answering = answer_approvals(ctx)
    try:
        await runner.run_task("Read twice")
    finally:
        answering.cancel()
    spans = [(s, e) for *_, s, e in _Timed.log]
    assert [n for _, n, *_ in _Timed.log] == [1, 2] and spans[0][1] <= spans[1][0]


async def test_stopping_the_member_stops_every_call_running_beside_it(db, run_row):
    calls = [_call(1, "slow_read", 1), _call(2, "slow_read", 2)]
    runner, *_ = _runner(db, run_row, calls)
    task = asyncio.create_task(runner.run_task("Read twice"))
    await asyncio.sleep(DELAY / 3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(DELAY * 1.5)
    assert task.cancelled() and _Timed.log == []  # neither call ran on after the member stopped


def test_the_read_only_and_council_tools_are_parallel_safe(settings):
    tools = {
        t.name: t
        for t in [
            *builtin_tools(),
            *web_tools(settings),
            *knowledge_tools(),
            *council_tools(),
            *spawn_tools(),
        ]
    }
    safe = {n for n, t in tools.items() if t.runs_in_parallel()}
    assert {"web_fetch", "notes_read", "artifact_read", "knowledge_search", "market_data"} <= safe
    assert {"knowledge_read", "memory_search"} <= safe
    assert {"ask_agent", "delegate_task", "send_message", "spawn_agents", "await_agents"} <= safe
    assert not {"artifact_write", "memory_write"} & safe
    assert WebSearch.parallel_safe  # registered only when a search provider is configured
