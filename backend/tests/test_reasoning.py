"""Each member's reasoning (chain of thought): streamed to the run owner as it arrives, recorded on
``thought.completed``, never passed to another member, and concealed for a secret stance."""

from __future__ import annotations

import json

import pytest

from argus.providers.anthropic import AnthropicProvider, _to_anthropic_messages, thinking_param
from argus.providers.gemini import GeminiProvider
from argus.providers.mock import MockProvider
from argus.providers.openai_compat import OpenAICompatProvider
from argus.providers.types import CompletionRequest, CompletionResponse, Message, StreamEvent, Usage
from argus.runtime.agent import MemberSpec, _ReasoningStream
from argus.runtime.events import EPHEMERAL_TYPES
from argus.runtime.graph import graph_for
from argus.runtime.guardrails import Guardrails
from argus.runtime.orchestrator import CouncilOrchestrator, Step
from argus.runtime.resume import load_resume_state
from argus.tools.base import ToolRegistry
from argus.tools.builtin import builtin_tools
from tests import test_limit_extension as limits
from tests.test_limit_extension import QUIET, make_ctx
from tests.test_providers import _fake_client, _msg

db, run_row = limits.db, limits.run_row  # the same fixtures

MARKER = "ZEBRA-REASONING-7731"


def _member(mid, name, role, overseer=False):
    return MemberSpec(
        id=mid,
        name=name,
        role=role,
        provider="fake",
        model="m",
        system_prompt=f"You are the {role}.",
        overseer=overseer,
    )


class StreamingLLM:
    """Streams ``script(request)``'s reasoning in small chunks, then its text; records requests."""

    def __init__(self, script, chunk=7):
        self.script = script
        self.chunk = chunk
        self.requests: list[CompletionRequest] = []

    async def complete(self, req, route, **kw):
        self.requests.append(req)
        return self.script(req)

    async def stream(self, req, route, **kw):
        self.requests.append(req)
        resp = self.script(req)
        for i in range(0, len(resp.thinking), self.chunk):
            yield StreamEvent(type="thinking", text=resp.thinking[i : i + self.chunk])
        if resp.content:
            yield StreamEvent(type="text", text=resp.content)
        yield StreamEvent(type="done", response=resp)


def answer(content="ok", thinking="", reasoning_tokens=None):
    return CompletionResponse(
        provider="fake",
        model="m",
        content=content,
        thinking=thinking,
        reasoning_tokens=reasoning_tokens,
        usage=Usage(input_tokens=10, output_tokens=10),
    )


def _council(db, run_row, script, rails=None, members=None):
    llm = StreamingLLM(script)
    ctx, events = make_ctx(db, run_row, rails or QUIET, llm)
    members = members or [_member("m0", "Cleo", "chair"), _member("m1", "Ada", "researcher"),
                          _member("m2", "Ben", "analyst"), _member("m3", "Mo", "monitor", overseer=True)]  # fmt: skip
    orch = CouncilOrchestrator(ctx, members, ToolRegistry(builtin_tools()))
    return orch, ctx, events, llm, {m.name: m for m in members}


def _of(events, type_):
    return [e for e in events if e.type == type_]


# ---------------------------------------------------------------- contract
def test_the_guardrail_defaults_to_auto_and_the_delta_is_ephemeral():
    assert Guardrails.resolve({}).oversight.reasoning == "auto"
    assert Guardrails.resolve({"oversight": {"reasoning": "on"}}).oversight.reasoning == "on"
    with pytest.raises(ValueError):
        Guardrails.resolve({"oversight": {"reasoning": "loud"}})
    assert "thought.reasoning.delta" in EPHEMERAL_TYPES
    assert CompletionRequest(model="m", messages=[]).reasoning == "auto"


async def test_reasoning_streams_as_deltas_and_is_recorded_on_thought_completed(db, run_row):
    why = "First check the sources, then weigh the 2024 figures against the targets."
    orch, _ctx, events, llm, m = _council(db, run_row, lambda req: answer("Done.", why, 42))
    await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    assert llm.requests[-1].reasoning == "auto"
    deltas = _of(events, "thought.reasoning.delta")
    assert len(deltas) > 1 and all(d.ephemeral for d in deltas)
    (done,) = _of(events, "thought.completed")
    assert {d.data["thought_id"] for d in deltas} == {done.data["thought_id"]}
    assert "".join(d.data["text"] for d in deltas) == why
    assert done.data["reasoning"] == why and done.data["reasoning_tokens"] == 42
    assert done.data["text"] == "Done."
    assert all(d.actor.member_id == "m1" for d in deltas)


async def test_a_turn_without_reasoning_records_an_empty_string(db, run_row):
    orch, _ctx, events, _llm, m = _council(db, run_row, lambda req: answer("Done."))
    await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    (done,) = _of(events, "thought.completed")
    assert done.data["reasoning"] == "" and done.data["reasoning_tokens"] is None
    assert not _of(events, "thought.reasoning.delta")


async def test_off_records_no_reasoning(db, run_row):
    rails = {"oversight": {"monitor_sensitivity": "off", "reasoning": "off"}}
    orch, _ctx, events, llm, m = _council(db, run_row, lambda req: answer("Done.", MARKER, 9), rails)
    await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    assert llm.requests[-1].reasoning == "off"
    assert not _of(events, "thought.reasoning.delta")
    (done,) = _of(events, "thought.completed")
    assert done.data["reasoning"] == ""
    assert MARKER not in json.dumps([e.data for e in events])


async def test_on_is_passed_to_the_provider(db, run_row):
    rails = {"oversight": {"monitor_sensitivity": "off", "reasoning": "on"}}
    orch, _ctx, _events, llm, m = _council(db, run_row, lambda req: answer("Done."), rails)
    await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    assert llm.requests[-1].reasoning == "on"


# ---------------------------------------------------------------- isolation
async def test_reasoning_never_reaches_another_member_the_board_the_graph_or_a_replay(db, run_row):
    def script(req):
        task = next(m.content for m in req.messages if m.role == "user")
        if "Gather evidence" in task.split("Your task:")[-1]:
            return answer("## Evidence\nGermany added 41 GW of solar in 2024.", f"Private: {MARKER}.")
        return answer("## Analysis\nThe grid is the constraint.", f"Ben thinks: {MARKER}-ben.")

    llm = StreamingLLM(script)
    ctx, events = make_ctx(db, run_row, QUIET, llm)
    ada, ben = _member("m1", "Ada", "researcher"), _member("m2", "Ben", "analyst")
    orch = CouncilOrchestrator(ctx, [ada, ben], ToolRegistry(builtin_tools()))
    orch.steps = [
        Step("s1", "Gather evidence", "Gather evidence", ada),
        Step("s2", "Analyse findings", "Analyse findings", ben, ["s1"]),
    ]
    graph = graph_for(ctx)
    await orch.execute()

    # It was recorded for the owner...
    completed = _of(events, "thought.completed")
    assert any(MARKER in e.data["reasoning"] for e in completed)
    # ...but none of Ben's prompts carry Ada's reasoning (system prompt, task, inbox, board).
    ben_reqs = [r for r in llm.requests if r.metadata.get("member_id") == "m2"]
    assert ben_reqs
    for r in ben_reqs:
        assert f"Private: {MARKER}" not in "\n".join(msg.content for msg in r.messages)
    # Ada's own later prompts do not get it back as text either (only native replay blocks would).
    for r in llm.requests:
        assert all("Private:" not in msg.content for msg in r.messages)
    bus = ctx.extra["council_bus"]
    shared = [str(bus.working_now(ben)), str(bus.working_now(ada)), json.dumps(ctx.notes, default=str)]
    shared += [bus.prompt_for(ben, lambda _n: True), bus.prompt_for(ada, lambda _n: True)]
    for etype in ("member.live", "a2a.message", "task.completed", "board.note", "step.status"):
        shared += [json.dumps(e.data, default=str) for e in _of(events, etype)]
    assert graph is not None
    await graph.flush()
    shared += [json.dumps([n.label, n.body]) for n in graph.nodes.values()]
    shared += list(graph._thoughts.values())
    assert graph._thoughts  # the graph did read the thoughts: their text only
    leaked = [s for s in shared if MARKER in s]
    assert not leaked, leaked
    # A resumed run replays answers, never reasoning.
    state = await load_resume_state(db.sessionmaker, ctx.run_id)
    replayed = json.dumps({k: list(v) for k, v in state.answers.items()})
    assert state.tasks and MARKER not in replayed


async def test_the_advocates_reasoning_is_concealed_even_when_split_across_chunks(db, run_row):
    secret = (
        "As the devil's advocate I was assigned to argue against this, so my role is to oppose "
        "the plan and argue the other side. "
    )
    why = secret + "Then look for the weakest figure. " * 8
    orch, _ctx, events, llm, _m = _council(db, run_row, lambda req: answer("Costs look understated.", why))
    llm.chunk = 3  # every giveaway phrase spans several chunks
    adv = await orch.debate.seat_devils_advocate()
    await orch.runner(adv).run_task("Work", step_title="Work")
    done = _of(events, "thought.completed")[-1]
    streamed = "".join(e.data["text"] for e in _of(events, "thought.reasoning.delta"))
    for text in (done.data["reasoning"], streamed):
        low = text.lower()
        assert "devil" not in low and "assigned to argue" not in low and "other side" not in low
        assert "role is to oppose" not in low
    assert streamed and done.data["reasoning"].startswith(streamed)
    assert "sceptic" in done.data["reasoning"] and "weakest figure" in done.data["reasoning"]


def test_the_reasoning_stream_passes_chunks_through_for_members_without_a_stance():
    plain = _member("m1", "Ada", "researcher")
    s = _ReasoningStream(plain)
    assert s.feed("devil's ") == "devil's " and s.feed("advocate") == "advocate"
    assert s.text == "devil's advocate"


# ---------------------------------------------------------------- providers
def test_anthropic_thinking_param_follows_the_model():
    # Claude 4.7+ and 5.x: adaptive thinking, with a readable summary (the default display omits it).
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-7", "claude-fable-5-1"):
        assert thinking_param(model, "on", 16_000) == {"type": "adaptive", "display": "summarized"}
    # Claude 4.6 summarizes by default and predates ``display``.
    assert thinking_param("claude-sonnet-4-6", "on", 16_000) == {"type": "adaptive"}
    # Before 4.6: a fixed budget, at least 1024 and below max_tokens; none when it cannot fit.
    assert thinking_param("claude-haiku-4-5", "on", 16_000) == {"type": "enabled", "budget_tokens": 8_000}
    assert thinking_param("claude-sonnet-4-5-20250929", "on", 4_000) == {
        "type": "enabled",
        "budget_tokens": 2_000,
    }
    assert thinking_param("claude-haiku-4-5", "on", 1_500) is None
    assert thinking_param("claude-3-5-haiku-latest", "on", 16_000) is None
    # Platform prefixes are ignored.
    assert thinking_param("us.anthropic.claude-opus-5-5", "on", 16_000)["type"] == "adaptive"
    # auto and off never change the request (current models reject {"type": "disabled"}).
    for mode in ("auto", "off"):
        assert thinking_param("claude-opus-5-5", mode, 16_000) is None


async def test_anthropic_on_sends_adaptive_summarized_thinking_without_sampling_params():
    client, calls = _fake_client(_msg(stop="tool_use"))
    p = AnthropicProvider(client=client)
    r = await p.complete(
        CompletionRequest(
            model="claude-opus-5-5",
            messages=[Message.user("q")],
            temperature=0.7,
            effort="high",
            reasoning="on",
        )
    )
    params = calls[0]
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "high"}  # effort stays the depth control
    assert "temperature" not in params and "top_p" not in params and "top_k" not in params
    assert r.thinking == "hmm"
    # The thinking block is replayed unchanged, signature and all, to the same model.
    replay = _to_anthropic_messages([Message.user("q"), r.as_message()])
    assert replay[1]["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}

    client, calls = _fake_client(_msg())
    await AnthropicProvider(client=client).complete(
        CompletionRequest(model="claude-opus-5-5", messages=[Message.user("q")])
    )
    assert "thinking" not in calls[0]  # auto: the request is unchanged


def test_gemini_on_asks_thinking_models_for_their_thoughts():
    p = GeminiProvider("k")
    on = CompletionRequest(model="gemini-2.5-pro", messages=[Message.user("q")], reasoning="on")
    assert p._body(on)["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    old = on.model_copy(update={"model": "gemini-2.0-flash"})
    assert "thinkingConfig" not in p._body(old)["generationConfig"]
    auto = on.model_copy(update={"reasoning": "auto"})
    assert "thinkingConfig" not in p._body(auto)["generationConfig"]
    data = {
        "candidates": [{"content": {"parts": [{"text": "why", "thought": True}, {"text": "answer"}]}}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "thoughtsTokenCount": 17},
    }
    r = p._parse("gemini-2.5-pro", data)
    assert r.thinking == "why" and r.content == "answer" and r.reasoning_tokens == 17


def test_openai_reports_reasoning_tokens_and_on_changes_nothing():
    p = OpenAICompatProvider(api_key="k")
    req = CompletionRequest(model="o4-mini", messages=[Message.user("q")], reasoning="on")
    body = p._body(req, stream=False)
    assert "reasoning" not in body and "include_reasoning" not in body
    assert p._reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": 64}}) == 64
    assert p._reasoning_tokens({"completion_tokens": 3}) is None


async def test_mock_council_turns_show_reasoning_unless_off():
    meta = {"phase": "execute", "role": "researcher", "topic": "solar"}
    mock = MockProvider()
    r = await mock.complete(CompletionRequest(model="mock-1", messages=[Message.user("go")], metadata=meta))
    again = await mock.complete(
        CompletionRequest(model="mock-1", messages=[Message.user("go")], metadata=meta)
    )
    assert r.thinking and r.thinking == again.thinking and "researcher" in r.thinking
    assert r.reasoning_tokens
    off = await mock.complete(
        CompletionRequest(model="mock-1", messages=[Message.user("go")], metadata=meta, reasoning="off")
    )
    assert off.thinking == ""


# ---------------------------------------------------------------- preserved thinking (block binding)
def _binding_failing_client(msg, failures=1):
    """A fake Anthropic client whose first ``failures`` requests fail with the preserved-thinking 400."""
    import httpx
    from anthropic import BadRequestError

    from tests.test_providers import _FakeStream

    calls = []

    def stream(**params):
        calls.append(params)
        if len(calls) <= failures:
            raise BadRequestError(
                "messages.3.content.0: Invalid `signature` in `thinking` block. The block is bound to a "
                "different conversation. Remove the block, or set "
                '`thinking.block_binding.prefix_mismatch_behavior` to "drop_block".',
                response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
                body=None,
            )
        return _FakeStream(msg, ())

    from types import SimpleNamespace

    api = SimpleNamespace(stream=stream)
    return SimpleNamespace(messages=api, beta=SimpleNamespace(messages=api)), calls


async def test_a_stale_thinking_block_is_dropped_instead_of_failing_the_turn():
    from tests.test_providers import _msg

    client, calls = _binding_failing_client(_msg())
    p = AnthropicProvider(client=client)
    r = await p.complete(CompletionRequest(model="claude-opus-5-5", messages=[Message.user("q")]))
    assert r.content == "Hi" and len(calls) == 2
    # First try: unchanged (an unenforced account keeps sending its blocks to the model).
    assert "thinking" not in calls[0]
    # The retry, and every request after it, asks the API to drop stale blocks.
    assert calls[1]["thinking"] == {
        "type": "adaptive",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert "thinking-binding-controls-2026-08-01" in calls[1]["betas"]
    assert "server-side-fallback-2026-07-01" in calls[1]["betas"]  # the refusal fallback stays
    await p.complete(CompletionRequest(model="claude-opus-5-5", messages=[Message.user("q")], reasoning="on"))
    assert calls[2]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    # A model that does not think unless asked gets no thinking turned on.
    await p.complete(CompletionRequest(model="claude-opus-4-8", messages=[Message.user("q")]))
    assert "thinking" not in calls[3]


async def test_the_retry_happens_once_and_other_400s_still_fail():
    import pytest as _pytest

    from argus.providers.base import ProviderError
    from tests.test_providers import _msg

    client, calls = _binding_failing_client(_msg(), failures=2)
    p = AnthropicProvider(client=client)
    with _pytest.raises(ProviderError):
        await p.complete(CompletionRequest(model="claude-opus-5-5", messages=[Message.user("q")]))
    assert len(calls) == 2  # one retry, not a loop


async def test_streaming_retries_a_stale_thinking_block_too():
    from tests.test_providers import _msg

    client, calls = _binding_failing_client(_msg())
    p = AnthropicProvider(client=client)
    events = [
        e async for e in p.stream(CompletionRequest(model="claude-sonnet-5-5", messages=[Message.user("q")]))
    ]
    assert events[-1].type == "done" and events[-1].response.content == "Hi"
    assert len(calls) == 2 and calls[1]["thinking"]["block_binding"] == {
        "prefix_mismatch_behavior": "drop_block"
    }
