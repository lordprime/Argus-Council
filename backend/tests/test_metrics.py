"""M5 (bounded label values) and M7 (failure-mode metrics)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from argus.cache.store import Cache, MemoryBackend
from argus.core.config import Settings
from argus.core.metrics import (
    LLM_CALLS,
    LLM_COST,
    LLM_ERRORS,
    LLM_FALLBACKS,
    LLM_TOKENS,
    REGISTRY,
    finish_label,
    intercept_reason_label,
    llm_model_label,
    method_label,
    tool_label,
)
from argus.providers.base import ProviderError
from argus.providers.mock import MockProvider, council_responder
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import LLMRouter, Route
from tests.test_runs import API, install, wait_status


def _value(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _series(prefix: str) -> list[dict[str, str]]:
    return [s.labels for m in REGISTRY.collect() for s in m.samples if s.name.startswith(prefix)]


# ------------------------------------------------------------ label helpers
def test_tool_label_bounds_names():
    class _T:
        def __init__(self, name: str, source: str) -> None:
            self.name, self.source = name, source

    assert tool_label(None) == "unknown"  # names the model invented
    assert tool_label(_T("web_fetch", "builtin")) == "web_fetch"
    assert tool_label(_T("mcp__srv__anything", "mcp")) == "mcp"  # server and tool names are user data
    assert tool_label(_T("remote_agent", "a2a")) == "remote_agent"


def test_intercept_reason_whitelist():
    for r in ("drift", "loop", "scope", "guardrail", "factual", "safety"):
        assert intercept_reason_label(r) == r
    assert intercept_reason_label("The agent wandered off into poetry") == "other"
    assert intercept_reason_label("") == "other"


def test_model_label_only_for_catalog_models():
    assert llm_model_label("claude-opus-5-5") == "claude-opus-5-5"
    assert llm_model_label("anthropic/claude-opus-5-5") == "claude-opus-5-5"
    assert llm_model_label("my-finetune-" + uuid.uuid4().hex) == "other"


def test_finish_and_method_labels():
    assert finish_label("stop") == "stop" and finish_label("tool_calls") == "tool_calls"
    assert finish_label("weird_provider_reason") == "other"
    assert method_label("GET") == "GET" and method_label("BREW") == "other"


# ------------------------------------------------------------ router records LLM metrics
def _router(**system) -> LLMRouter:
    settings = Settings(env="test", secret_key="k" * 40, llm_max_retries=2)
    return LLMRouter(settings, Cache(MemoryBackend()), system={"mock": MockProvider(), **system})


def _req(text: str = "hi") -> CompletionRequest:
    return CompletionRequest(model="x", messages=[Message.user(text)], temperature=0.7)


class _Failing(MockProvider):
    kind = "openai"

    def __init__(self, name: str, status: int, retryable: bool) -> None:
        super().__init__(name)
        self.status, self.retryable = status, retryable

    async def complete(self, request):  # type: ignore[no-untyped-def]
        raise ProviderError("boom", provider=self.name, status=self.status, retryable=self.retryable)


async def test_router_labels_llm_metrics_by_provider_kind_and_catalog_model():
    user_named = f"my-provider-{uuid.uuid4().hex[:6]}"
    router = _router(**{user_named: MockProvider(user_named)})
    before = _value("argus_llm_calls_total", {"provider": "mock", "model": "other", "finish": "stop"})
    await router.complete(_req(), Route(user_named, f"custom-model-{uuid.uuid4().hex[:6]}"))
    after = _value("argus_llm_calls_total", {"provider": "mock", "model": "other", "finish": "stop"})
    assert after == before + 1
    assert not [s for s in _series("argus_llm_") if s.get("provider") == user_named]
    assert not [s for s in _series("argus_llm_") if s.get("model", "").startswith("custom-model")]
    assert LLM_TOKENS and LLM_COST and LLM_CALLS  # exported names stay importable


async def test_router_counts_stream_completions():
    router = _router()
    before = _value("argus_llm_calls_total", {"provider": "mock", "model": "other", "finish": "stop"})
    async for _ in router.stream(_req(), Route("mock", "m")):
        pass
    assert (
        _value("argus_llm_calls_total", {"provider": "mock", "model": "other", "finish": "stop"})
        == before + 1
    )


async def test_router_counts_provider_errors_and_fallbacks():
    router = _router(bad=_Failing("bad", 503, True))
    labels = {"provider_kind": "openai", "status_class": "5xx", "retryable": "true"}
    errors_before = _value("argus_llm_errors_total", labels)
    fallbacks_before = _value("argus_llm_fallbacks_total", {"kind": "route"})
    resp = await router.complete(_req(), Route("bad", "m"), fallbacks=[Route("mock", "m")])
    assert resp.provider == "mock"
    # Every failed attempt counts (llm_max_retries=2), and the fallback that served the call.
    assert _value("argus_llm_errors_total", labels) == errors_before + 2
    assert _value("argus_llm_fallbacks_total", {"kind": "route"}) == fallbacks_before + 1
    assert LLM_ERRORS and LLM_FALLBACKS


async def test_router_counts_stream_errors():
    router = _router(bad=_Failing("bad", 400, False))
    labels = {"provider_kind": "openai", "status_class": "4xx", "retryable": "false"}
    before = _value("argus_llm_errors_total", labels)
    with pytest.raises(ProviderError):
        async for _ in router.stream(_req(), Route("bad", "m")):
            pass
    assert _value("argus_llm_errors_total", labels) == before + 1


async def test_cached_responses_are_not_counted_as_calls():
    router = _router()
    req = CompletionRequest(model="x", messages=[Message.user("cache me")], temperature=0)
    await router.complete(req, Route("mock", "m"))
    before = _value("argus_llm_calls_total", {"provider": "mock", "model": "other", "finish": "stop"})
    hit = await router.complete(req, Route("mock", "m"))
    assert hit.cached
    assert _value("argus_llm_calls_total", {"provider": "mock", "model": "other", "finish": "stop"}) == before


# ------------------------------------------------------------ runtime call sites
async def test_hallucinated_tool_names_do_not_create_series(app, client, auth):
    invented = f"hallucinated_{uuid.uuid4().hex[:8]}"
    state = {"sent": False}

    def responder(req: CompletionRequest):  # type: ignore[no-untyped-def]
        if req.tools and not state["sent"]:
            state["sent"] = True
            return {"content": "", "tool_calls": [{"name": invented, "arguments": {}}]}
        return council_responder(req)

    install(app, "halluc", responder)
    before = _value("argus_tool_calls_total", {"tool": "unknown", "source": "builtin", "status": "error"})
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "t", "mode": "chat", "size": 2, "provider": "halluc", "model": "m"},
    )
    await wait_status(client, auth, r.json()["id"])
    assert state["sent"], "the scripted tool call was never requested"
    assert not [s for s in _series("argus_tool_calls") if s.get("tool") == invented]
    assert (
        _value("argus_tool_calls_total", {"tool": "unknown", "source": "builtin", "status": "error"})
        == before + 1
    )


async def test_run_duration_observed_on_finish(client, auth):
    labels = {"mode": "chat", "status": "completed"}
    before = _value("argus_run_duration_seconds_count", labels)
    r = await client.post(API, headers=auth, json={"topic": "d", "mode": "chat", "size": 2})
    await wait_status(client, auth, r.json()["id"])
    # Metrics are recorded at the end of the run task, just after the terminal status is committed.
    for _ in range(100):
        if _value("argus_run_duration_seconds_count", labels) > before:
            break
        await asyncio.sleep(0.02)
    assert _value("argus_run_duration_seconds_count", labels) == before + 1


async def test_event_persist_failures_counted():
    from argus.core.metrics import EVENT_PERSIST_FAILURES, counts_failures

    @counts_failures(EVENT_PERSIST_FAILURES)
    async def persist() -> None:
        raise RuntimeError("db down")

    before = _value("argus_event_persist_failures_total", {})
    with pytest.raises(RuntimeError):
        await persist()
    assert _value("argus_event_persist_failures_total", {}) == before + 1
    # And the real sink is wired to it.
    from argus.runtime.events import EventSink

    assert getattr(EventSink._persist, "__wrapped__", None) is not None


async def test_webhook_delivery_metrics():
    from argus.core.metrics import observes_webhook_delivery
    from argus.services.webhooks import WebhookDispatcher

    @observes_webhook_delivery
    async def deliver(ok: bool) -> bool:
        if ok is None:
            raise RuntimeError("decrypt failed")
        return ok

    snap = {r: _value("argus_webhook_deliveries_total", {"result": r}) for r in ("ok", "failed", "error")}
    count = _value("argus_webhook_delivery_seconds_count", {})
    await deliver(True)
    await deliver(False)
    with pytest.raises(RuntimeError):
        await deliver(None)
    for r in ("ok", "failed", "error"):
        assert _value("argus_webhook_deliveries_total", {"result": r}) == snap[r] + 1
    assert _value("argus_webhook_delivery_seconds_count", {}) == count + 3
    assert getattr(WebhookDispatcher.deliver, "__wrapped__", None) is not None


async def test_retention_success_metrics(app):
    before = _value("argus_retention_last_success_timestamp", {})
    await app.state.retention.run()
    assert _value("argus_retention_last_success_timestamp", {}) > max(before, 1)
    text = REGISTRY.get_sample_value("argus_retention_deleted_total", {"kind": "runs"})
    assert text is not None


async def test_approvals_pending_gauge_reads_live_runs(app, client):
    import asyncio
    from types import SimpleNamespace

    loop = asyncio.get_running_loop()
    done = loop.create_future()
    done.set_result({})
    approvals = {
        "a": SimpleNamespace(future=loop.create_future()),
        "b": SimpleNamespace(future=loop.create_future()),
        "c": SimpleNamespace(future=done),
    }
    fake = SimpleNamespace(ctx=SimpleNamespace(approvals=approvals), task=None)
    app.state.runs.live[uuid.uuid4()] = fake
    try:
        await client.get("/metrics")
        assert _value("argus_approvals_pending", {}) == 2
    finally:
        app.state.runs.live.clear()
    await client.get("/metrics")
    assert _value("argus_approvals_pending", {}) == 0


async def test_sse_stream_gauge_tracks_open_streams():
    from argus.core.metrics import SSE_STREAMS, tracked_stream

    async def body():
        yield b"a"
        yield b"b"

    base = _value("argus_sse_streams", {})
    it = tracked_stream(body(), SSE_STREAMS)
    assert await it.__anext__() == b"a"
    assert _value("argus_sse_streams", {}) == base + 1
    await it.aclose()
    assert _value("argus_sse_streams", {}) == base


async def test_sse_endpoint_gauge_returns_to_baseline(client, auth):
    r = await client.post(API, headers=auth, json={"topic": "s", "mode": "chat", "size": 2})
    rid = r.json()["id"]
    await wait_status(client, auth, rid)
    base = _value("argus_sse_streams", {})
    resp = await client.get(f"{API}/{rid}/events/stream", headers=auth)
    assert resp.status_code == 200 and b"run.status" in resp.content
    assert _value("argus_sse_streams", {}) == base


async def test_process_and_gc_collectors_registered(client):
    text = (await client.get("/metrics")).text
    assert "process_resident_memory_bytes" in text or "process_cpu_seconds_total" in text
    assert "python_gc_objects_collected_total" in text
    assert "python_info" in text


async def test_failure_mode_metrics_are_exposed(client):
    text = (await client.get("/metrics")).text
    for name in (
        "argus_llm_errors_total",
        "argus_llm_fallbacks_total",
        "argus_oversight_review_failures_total",
        "argus_webhook_deliveries_total",
        "argus_webhook_delivery_seconds",
        "argus_event_persist_failures_total",
        "argus_sse_streams",
        "argus_approvals_pending",
        "argus_run_duration_seconds",
        "argus_retention_deleted_total",
        "argus_retention_last_success_timestamp",
        "argus_cache_degraded_total",
        "argus_protocol_blocking_waits",
    ):
        assert f"# TYPE {name}" in text, name
