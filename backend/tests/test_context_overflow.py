"""A provider refusing a prompt for its size makes the agent compact harder and try again, the way
Claude Code recovers, instead of failing the step (and, with it, the run)."""

import pytest

from argus.providers.base import ProviderError
from argus.runtime.agent import is_context_overflow
from tests.test_debate import _council, _of, db, run_row  # noqa: F401  (fixtures)
from tests.test_limit_extension import QUIET, reply


@pytest.mark.parametrize(
    "message",
    [
        "prompt is too long: 212345 tokens > 200000 maximum",  # Anthropic
        "This model's maximum context length is 128000 tokens.",  # OpenAI
        "context_length_exceeded",
        "The input token count (1100000) exceeds the maximum number of tokens allowed",  # Gemini-ish
        "Request too large for gpt-5",
    ],
)
def test_context_overflow_is_recognised(message):
    assert is_context_overflow(ProviderError(message, status=400))


def test_other_errors_are_not_overflow():
    assert not is_context_overflow(ProviderError("invalid api key", status=401))
    assert not is_context_overflow(ProviderError("rate limited", status=429))
    assert not is_context_overflow(ProviderError("prompt is too long", status=500))


async def test_an_overflow_is_compacted_and_retried(db, run_row):  # noqa: F811
    calls = []

    def script(req):
        calls.append(1)
        if len(calls) == 1:
            return ProviderError("prompt is too long: 250000 tokens > 200000 maximum", status=400)
        return reply("Done.")

    orch, _, events, _, m = _council(db, run_row, script, QUIET)
    out = await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    assert out.ok and out.text == "Done."
    (compacted,) = _of(events, "context.compacted")
    assert compacted["reason"] == "context_overflow" and compacted["member_id"] == m["Ada"].id


async def test_an_overflow_that_persists_still_fails(db, run_row):  # noqa: F811
    def script(req):
        return ProviderError("prompt is too long", status=400)

    orch, _, events, _, m = _council(db, run_row, script, QUIET)
    with pytest.raises(ProviderError):
        await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    assert len(_of(events, "context.compacted")) == 2


# ---------------------------------------------------------------- budget pressure and the compressor
def test_the_token_ceiling_counts_the_raises_still_allowed(db, run_row):  # noqa: F811
    from argus.providers.types import Usage

    _, ctx, *_ = _council(db, run_row, lambda req: reply("ok"), QUIET)
    # 2,000,000 tokens and three raises of half the starting limit: the 5,000,000 a run stops at.
    assert ctx.token_ceiling() == 5_000_000
    ctx.engine.rails.budget.buffer_tokens = 500_000
    assert ctx.token_ceiling() == 5_500_000
    ctx.extension_declined.add("budget.tokens")
    assert ctx.token_ceiling() == 2_500_000
    ctx.engine.usage = Usage(input_tokens=2_000_000, output_tokens=500_000)
    assert ctx.token_pressure() == 1.0


async def test_history_shrinks_as_the_run_nears_its_token_ceiling(db, run_row):  # noqa: F811
    from argus.providers.types import Usage

    orch, ctx, events, _, m = _council(db, run_row, lambda req: reply("ok"), QUIET)
    runner = orch.runner(m["Ada"])
    policy = ctx.engine.rails.context
    ctx.engine.usage = Usage(input_tokens=2_000_000)  # 40% of 5M: no pressure yet
    assert await runner._pressured_budget(policy) == (120_000, 3)
    ctx.engine.usage = Usage(input_tokens=4_000_000)  # 80%: halfway from the start to the ceiling
    assert await runner._pressured_budget(policy) == (84_000, 1)
    ctx.engine.usage = Usage(input_tokens=5_000_000)  # at the ceiling: 40% of the usual budget
    assert await runner._pressured_budget(policy) == (48_000, 1)
    await runner._pressured_budget(policy)  # same tier again: no second event
    assert [e["pressure"] for e in _of(events, "context.pressure")] == [0.8, 1.0]
    policy.pressure_start = 1.0  # off
    assert await runner._pressured_budget(policy) == (120_000, 3)


async def test_the_compressor_runs_on_the_cheapest_strong_model(db, run_row, monkeypatch):  # noqa: F811
    import dataclasses

    import argus.tools.spawn as spawn
    from argus.providers.types import Message
    from tests.test_debate import _pool_of

    monkeypatch.setattr(spawn, "_pool", _pool_of("claude-opus-5-5", "claude-haiku-5-5"))
    models = []

    def script(req):
        models.append((req.metadata.get("phase"), req.model))
        return reply("Summary of the earlier work.")

    orch, ctx, *_ = _council(db, run_row, script, QUIET)
    ida = dataclasses.replace(orch.members[1], provider="anthropic", model="claude-opus-5-5")
    runner = orch.runner(ida)
    messages = [Message.system("s"), Message.user("Your task:\nWork")]
    for i in range(4):
        messages += [
            Message.assistant(f"turn {i} " + "x" * 2_000),
            Message.user(f"result {i} " + "y" * 2_000),
        ]
    assert await runner._condense(messages, "Your task:\nWork", None)
    assert models == [("condense", "claude-haiku-5-5")]
    ctx.engine.rails.context.compressor = "seat"
    models.clear()
    messages += [Message.assistant("a" * 2_000), Message.user("b" * 2_000)] * 3
    await runner._condense(messages, "Your task:\nWork", None)
    assert models == [("condense", "claude-opus-5-5")]
