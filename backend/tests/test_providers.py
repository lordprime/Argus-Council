import json
from types import SimpleNamespace

import httpx
import pytest

from argus.providers.anthropic import AnthropicProvider, _to_anthropic_messages
from argus.providers.base import ProviderError, with_retries
from argus.providers.gemini import GeminiProvider, _clean_schema
from argus.providers.mock import MockProvider
from argus.providers.openai_compat import OpenAICompatProvider
from argus.providers.pricing import estimate_cost
from argus.providers.types import CompletionRequest, Message, ToolCall, ToolSpec, Usage

TOOL = ToolSpec(
    name="lookup",
    description="look",
    input_schema={
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    },
)


def req(**kw):
    base = dict(model="m", messages=[Message.system("sys"), Message.user("hi")])
    base.update(kw)
    return CompletionRequest(**base)


# ------------------------------------------------------------------ openai-compatible
def openai_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_complete_with_tools():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "gpt-x",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            },
        )

    p = OpenAICompatProvider(base_url="https://api.test/v1", api_key="sk-1", client=openai_transport(handler))
    r = await p.complete(req(tools=[TOOL], temperature=0.2))
    assert seen["auth"] == "Bearer sk-1"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["tools"][0]["function"]["name"] == "lookup"
    assert seen["body"]["temperature"] == 0.2
    assert r.finish_reason == "tool_calls"
    assert r.tool_calls[0].arguments == {"q": "x"}
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (6, 4, 3)


async def test_openai_tool_roundtrip_messages():
    def handler(request):
        body = json.loads(request.content)
        assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"q": "x"}'
        assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "result"}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        )

    p = OpenAICompatProvider(base_url="https://x/v1", client=openai_transport(handler))
    msgs = [
        Message.system("s"),
        Message.user("u"),
        Message.assistant("", [ToolCall(id="c1", name="lookup", arguments={"q": "x"})]),
        Message.tool_result("c1", "result"),
    ]
    r = await p.complete(CompletionRequest(model="m", messages=msgs))
    assert r.content == "done"


async def test_openai_stream_accumulates_tool_calls():
    chunks = [
        {"model": "gpt-x", "choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "c9", "function": {"name": "look", "arguments": '{"q"'}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"name": "up", "arguments": ': "z"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    p = OpenAICompatProvider(
        base_url="https://x/v1", client=openai_transport(lambda r: httpx.Response(200, text=sse))
    )
    events = [e async for e in p.stream(req())]
    assert "".join(e.text for e in events if e.type == "text") == "Hello"
    done = events[-1].response
    assert done.tool_calls[0].name == "lookup" and done.tool_calls[0].arguments == {"q": "z"}
    assert done.finish_reason == "tool_calls" and done.usage.output_tokens == 2


async def test_openai_errors_classified():
    p = OpenAICompatProvider(
        base_url="https://x/v1",
        client=openai_transport(lambda r: httpx.Response(429, text="slow", headers={"retry-after": "2"})),
    )
    with pytest.raises(ProviderError) as ei:
        await p.complete(req())
    assert ei.value.retryable and ei.value.retry_after == 2.0
    p = OpenAICompatProvider(
        base_url="https://x/v1", client=openai_transport(lambda r: httpx.Response(400, text="bad"))
    )
    with pytest.raises(ProviderError) as ei:
        await p.complete(req())
    assert not ei.value.retryable


async def test_openai_bad_tool_json_does_not_crash():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [{"id": "c", "function": {"name": "lookup", "arguments": "{oops"}}]
                        },
                    }
                ]
            },
        )

    p = OpenAICompatProvider(base_url="https://x/v1", client=openai_transport(handler))
    r = await p.complete(req())
    assert r.tool_calls[0].arguments["_error"] == "invalid_json"


async def test_openai_list_models():
    p = OpenAICompatProvider(
        base_url="https://x/v1",
        client=openai_transport(lambda r: httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})),
    )
    assert await p.list_models() == ["a", "b"]


# ------------------------------------------------------------------ gemini
async def test_gemini_complete_and_schema_cleaning():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": "thinking...", "thought": True},
                                {"text": "answer"},
                                {"functionCall": {"name": "lookup", "args": {"q": "k"}}},
                            ]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3, "thoughtsTokenCount": 2},
            },
        )

    p = GeminiProvider("gk", client=openai_transport(handler))
    r = await p.complete(req(model="gemini-x", tools=[TOOL]))
    assert seen["url"].endswith("/models/gemini-x:generateContent")
    assert seen["key"] == "gk"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    params = seen["body"]["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in params
    assert r.content == "answer" and r.thinking == "thinking..."
    assert r.tool_calls[0].arguments == {"q": "k"} and r.finish_reason == "tool_calls"
    assert r.usage.output_tokens == 5


def test_gemini_clean_schema_nested():
    assert _clean_schema(
        {"$schema": "x", "properties": {"a": {"additionalProperties": True, "type": "object"}}}
    ) == {"properties": {"a": {"type": "object"}}}


async def test_gemini_stream():
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": "a"}]}}]},
        {
            "candidates": [{"content": {"parts": [{"text": "b"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
        },
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    p = GeminiProvider("k", client=openai_transport(lambda r: httpx.Response(200, text=sse)))
    events = [e async for e in p.stream(req(model="g"))]
    assert [e.text for e in events if e.type == "text"] == ["a", "b"]
    assert events[-1].response.content == "ab"


# ------------------------------------------------------------------ anthropic
class _FakeStream:
    def __init__(self, msg, events=()):
        self.msg, self.events = msg, list(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e

        return gen()

    async def get_final_message(self):
        return self.msg


def _fake_client(msg, events=()):
    calls = []

    def stream(**params):
        calls.append(params)
        return _FakeStream(msg, events)

    api = SimpleNamespace(stream=stream)
    return SimpleNamespace(messages=api, beta=SimpleNamespace(messages=api)), calls


def _block(**kw):
    return SimpleNamespace(**kw, model_dump=lambda exclude_none=True: kw)


def _msg(model="claude-opus-5-5", stop="end_turn"):
    return SimpleNamespace(
        model=model,
        stop_reason=stop,
        content=[
            _block(type="thinking", thinking="hmm", signature="sig"),
            _block(type="text", text="Hi"),
            _block(type="tool_use", id="tu1", name="lookup", input={"q": "1"}),
        ],
        usage=SimpleNamespace(
            input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


async def test_anthropic_params_and_conversion():
    client, calls = _fake_client(_msg(stop="tool_use"))
    p = AnthropicProvider(client=client)
    r = await p.complete(
        req(model="claude-opus-5-5", tools=[TOOL], temperature=0.7, effort="high", stop=["X"])
    )
    params = calls[0]
    assert params["system"] == "sys"
    assert "temperature" not in params  # current Claude models reject sampling params
    assert params["output_config"] == {"effort": "high"}
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["tools"][0]["input_schema"]["required"] == ["q"]
    assert r.content == "Hi" and r.thinking == "hmm" and r.finish_reason == "tool_calls"
    assert r.tool_calls[0].id == "tu1"
    assert r.usage.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    # native blocks preserved for lossless replay (thinking signature intact)
    replay = _to_anthropic_messages([Message.user("q"), r.as_message()])
    assert replay[1]["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}


async def test_anthropic_no_fallback_for_other_models():
    client, calls = _fake_client(_msg(model="claude-haiku-4-5"))
    await AnthropicProvider(client=client).complete(req(model="claude-haiku-4-5"))
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


def test_anthropic_tool_results_merge():
    msgs = [
        Message.user("u"),
        Message.assistant("", [ToolCall(id="a", name="t"), ToolCall(id="b", name="t")]),
        Message.tool_result("a", "1"),
        Message.tool_result("b", "2", is_error=True),
    ]
    out = _to_anthropic_messages(msgs)
    assert len(out) == 3
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]
    assert out[2]["content"][1]["is_error"] is True


async def test_anthropic_stream_events():
    events = [
        SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking="t")
        ),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="H")),
        SimpleNamespace(type="message_stop"),
    ]
    client, _ = _fake_client(_msg(), events)
    out = [e async for e in AnthropicProvider(client=client).stream(req(model="claude-opus-5-5"))]
    assert [e.type for e in out] == ["thinking", "text", "tool_call", "done"]


async def test_anthropic_refusal_maps():
    client, _ = _fake_client(_msg(stop="refusal"))
    r = await AnthropicProvider(client=client).complete(req(model="claude-opus-5-5"))
    assert r.finish_reason == "refusal"


# ------------------------------------------------------------------ shared
async def test_with_retries():
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ProviderError("x", retryable=True)
        return "ok"

    async def nosleep(_):
        pass

    assert await with_retries(flaky, attempts=3, sleep=nosleep) == "ok"
    attempts.clear()

    async def fatal():
        attempts.append(1)
        raise ProviderError("bad", retryable=False)

    with pytest.raises(ProviderError):
        await with_retries(fatal, attempts=5, sleep=nosleep)
    assert len(attempts) == 1


async def test_mock_script_and_echo():
    m = MockProvider(
        script=["one", {"content": "", "tool_calls": [{"name": "lookup", "arguments": {"q": 1}}]}]
    )
    assert (await m.complete(req())).content == "one"
    r = await m.complete(req())
    assert r.finish_reason == "tool_calls" and r.tool_calls[0].name == "lookup"
    r1, r2 = await m.complete(req()), await m.complete(req())
    assert r1.content == r2.content and "hi" in r1.content


def test_pricing():
    assert estimate_cost("unknown-local", Usage(input_tokens=10**6)) == 0
    assert (
        estimate_cost("anthropic/claude-sonnet-5-5", Usage(input_tokens=10**6, output_tokens=10**6)) == 12.0
    )
    u = Usage(input_tokens=1, output_tokens=2, cost_usd=0.5) + Usage(input_tokens=3, cost_usd=0.25)
    assert (u.input_tokens, u.output_tokens, u.cost_usd, u.total_tokens) == (4, 2, 0.75, 6)


async def test_openai_sends_max_completion_tokens_and_drops_temperature_for_reasoning_models():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    p = OpenAICompatProvider(
        api_key="sk-1", client=openai_transport(handler)
    )  # the official API (kind openai)
    await p.complete(req(model="gpt-5", max_tokens=64, temperature=0.2))
    await p.complete(req(model="gpt-4.1", max_tokens=64, temperature=0.2))
    assert bodies[0]["max_completion_tokens"] == 64 and "max_tokens" not in bodies[0]
    assert "temperature" not in bodies[0]  # reasoning models only take the default
    assert bodies[1]["max_completion_tokens"] == 64 and bodies[1]["temperature"] == 0.2
    # Other OpenAI-compatible servers keep the classic parameter.
    local = OpenAICompatProvider(base_url="https://x/v1", kind="ollama", client=openai_transport(handler))
    await local.complete(req(model="llama3", max_tokens=32))
    assert bodies[2]["max_tokens"] == 32 and "max_completion_tokens" not in bodies[2]


UNSUPPORTED_MAX_TOKENS = {
    "error": {
        "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead.",
        "type": "invalid_request_error",
        "param": "max_tokens",
        "code": "unsupported_parameter",
    }
}
UNSUPPORTED_TEMPERATURE = {
    "error": {
        "message": "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported.",
        "param": "temperature",
        "code": "unsupported_value",
    }
}


async def test_openai_compatible_endpoint_adapts_to_rejected_parameters_once():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json=UNSUPPORTED_MAX_TOKENS)
        if "temperature" in body:
            return httpx.Response(400, json=UNSUPPORTED_TEMPERATURE)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    # A proxy in front of an OpenAI reasoning model, under a name the provider does not recognise.
    p = OpenAICompatProvider(
        base_url="https://proxy/v1", kind="openai_compatible", client=openai_transport(handler)
    )
    r = await p.complete(req(model="team/reasoner", max_tokens=50, temperature=0.2))
    assert r.content == "ok" and len(bodies) == 3
    assert bodies[-1]["max_completion_tokens"] == 50 and "temperature" not in bodies[-1]
    # Learned per model: the next call goes straight through.
    await p.complete(req(model="team/reasoner", max_tokens=50, temperature=0.2))
    assert len(bodies) == 4

    # Streaming adapts the same way (before anything is streamed).
    sse = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]})
        + "\n\ndata: [DONE]\n\n"
    )

    def stream_handler(request):
        body = json.loads(request.content)
        if "max_tokens" in body:
            return httpx.Response(400, json=UNSUPPORTED_MAX_TOKENS)
        return httpx.Response(200, text=sse)

    s = OpenAICompatProvider(
        base_url="https://proxy/v1", kind="openai_compatible", client=openai_transport(stream_handler)
    )
    events = [e async for e in s.stream(req(model="other", max_tokens=10))]
    assert "".join(e.text for e in events if e.type == "text") == "hi"


async def test_openai_unrelated_400_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            400, json={"error": {"message": "Invalid 'messages': empty", "param": "messages"}}
        )

    p = OpenAICompatProvider(api_key="sk-1", client=openai_transport(handler))
    with pytest.raises(ProviderError) as err:
        await p.complete(req(model="gpt-5"))
    assert err.value.status == 400 and len(calls) == 1
