"""Prometheus metrics for the API and the council runtime.

Label values must come from small, fixed sets. Anything that users, remote servers or models can
name (tool names, provider config names, model strings, intercept reasons, HTTP methods, URL paths)
goes through one of the ``*_label`` helpers below, which fold unknown values into ``other`` or
``unknown``. ``safe_label`` only bounds length and must not be used for new labels.
"""

from __future__ import annotations

import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)

REGISTRY = CollectorRegistry(auto_describe=True)
# Process memory / FDs / CPU, interpreter and GC stats (alerting on the container memory limit).
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

HTTP_REQUESTS = Counter(
    "argus_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY
)
HTTP_LATENCY = Histogram(
    "argus_http_request_seconds",
    "HTTP request latency",
    ["method", "route"],
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
RUNS_STARTED = Counter("argus_runs_started_total", "Council runs started", ["mode"], registry=REGISTRY)
RUNS_FINISHED = Counter(
    "argus_runs_finished_total", "Council runs finished", ["mode", "status"], registry=REGISTRY
)
RUNS_ACTIVE = Gauge("argus_runs_active", "Council runs executing in this process", registry=REGISTRY)
RUN_DURATION = Histogram(
    "argus_run_duration_seconds",
    "Council run wall-clock runtime, by final status",
    ["mode", "status"],
    registry=REGISTRY,
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200),
)
# Labels: provider = provider *kind* (anthropic, openai, ...), model = catalog model or "other".
LLM_CALLS = Counter(
    "argus_llm_calls_total", "LLM completions", ["provider", "model", "finish"], registry=REGISTRY
)
LLM_TOKENS = Counter(
    "argus_llm_tokens_total", "LLM tokens", ["provider", "model", "direction"], registry=REGISTRY
)
LLM_COST = Counter(
    "argus_llm_cost_usd_total", "Estimated LLM spend (USD)", ["provider", "model"], registry=REGISTRY
)
LLM_LATENCY = Histogram(
    "argus_llm_seconds",
    "LLM call latency",
    ["provider"],
    registry=REGISTRY,
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 40, 80, 160),
)
TOOL_CALLS = Counter("argus_tool_calls_total", "Tool calls", ["tool", "source", "status"], registry=REGISTRY)
TOOL_LATENCY = Histogram(
    "argus_tool_seconds",
    "Tool call latency",
    ["source"],
    registry=REGISTRY,
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
INTERCEPTS = Counter(
    "argus_intercepts_total", "Thought intercepts", ["by", "reason", "action"], registry=REGISTRY
)
VIOLATIONS = Counter(
    "argus_guardrail_violations_total", "Guardrail violations", ["rule", "action"], registry=REGISTRY
)
APPROVALS = Counter("argus_approvals_total", "Approval decisions", ["decision"], registry=REGISTRY)
APPROVALS_PENDING = Gauge(
    "argus_approvals_pending", "Approval requests waiting for a decision in this process", registry=REGISTRY
)
PROTOCOL_CALLS = Counter(
    "argus_protocol_calls_total", "MCP/A2A/ACP calls", ["protocol", "direction", "status"], registry=REGISTRY
)
PROTOCOL_BLOCKING_WAITS = Gauge(
    "argus_protocol_blocking_waits",
    "Inbound MCP/A2A/ACP calls currently waiting for a run to finish",
    registry=REGISTRY,
)
PROTOCOL_WAITS_SHED = Counter(
    "argus_protocol_blocking_waits_shed_total",
    "Blocking protocol calls answered immediately because the wait limit was reached",
    ["protocol"],
    registry=REGISTRY,
)

# LLM failure modes (recorded by the router, so agents, overseers and /llm/complete all count).
LLM_ERRORS = Counter(
    "argus_llm_errors_total",
    "LLM provider errors (every failed attempt, including retried ones)",
    ["provider_kind", "status_class", "retryable"],
    registry=REGISTRY,
)
LLM_FALLBACKS = Counter(
    "argus_llm_fallbacks_total",
    "LLM calls served by a fallback (another route, or complete() after a failed stream)",
    ["kind"],
    registry=REGISTRY,
)
OVERSIGHT_FAILURES = Counter(
    "argus_oversight_review_failures_total",
    "Overseer reviews that failed (the thought passed unreviewed)",
    ["role"],
    registry=REGISTRY,
)

# Delivery, persistence and streaming.
WEBHOOK_DELIVERIES = Counter(
    "argus_webhook_deliveries_total",
    "Webhook deliveries after all retries (result: ok, failed, error)",
    ["result"],
    registry=REGISTRY,
)
WEBHOOK_DELIVERY_SECONDS = Histogram(
    "argus_webhook_delivery_seconds",
    "Webhook delivery time including retries",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
EVENT_PERSIST_FAILURES = Counter(
    "argus_event_persist_failures_total", "Run events that failed to persist", registry=REGISTRY
)
SSE_STREAMS = Gauge("argus_sse_streams", "Open run event streams (SSE) in this process", registry=REGISTRY)

# Data retention.
RETENTION_DELETED = Counter(
    "argus_retention_deleted_total",
    "Rows and blob directories pruned by retention",
    ["kind"],
    registry=REGISTRY,
)
RETENTION_LAST_SUCCESS = Gauge(
    "argus_retention_last_success_timestamp",
    "Unix time of the last successful retention pass",
    registry=REGISTRY,
)

# Redis degradation: operations that fell back to a local substitute or were skipped (M8).
CACHE_DEGRADED = Counter(
    "argus_cache_degraded_total",
    "Cache/Redis operations that failed and were degraded (op: rate_limit, cache_read, cache_write, ...)",
    ["op"],
    registry=REGISTRY,
)


# ------------------------------------------------------------ label helpers
UNMATCHED_ROUTE = "unmatched"
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
INTERCEPT_REASONS = frozenset({"drift", "loop", "scope", "guardrail", "factual", "safety"})
_FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "error", "refusal", "content_filter"})


def route_template(request: Any) -> str:
    """The matched route's path template (``/api/v1/runs/{run_id}``), never the raw URL path."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or UNMATCHED_ROUTE


def method_label(method: str) -> str:
    """HTTP methods are client-chosen strings; anything non-standard is folded into ``other``."""
    return method if method in _HTTP_METHODS else "other"


def tool_label(tool: Any) -> str:
    """Tool label for a resolved tool (or ``None`` when the model named a tool that does not exist).

    Built-in, skill and remote-agent tools have code-defined names. MCP tool names embed a
    user-chosen server name and whatever the remote server advertises, so they collapse to ``mcp``.
    """
    if tool is None:
        return "unknown"
    if getattr(tool, "source", "builtin") == "mcp":
        return "mcp"
    return str(tool.name)[:64]


def intercept_reason_label(reason: str | None) -> str:
    """Overseer reasons are LLM-written; only the documented set becomes a label value."""
    value = (reason or "").strip().lower()
    return value if value in INTERCEPT_REASONS else "other"


def role_label(role: str | None) -> str:
    from argus.agents.roles import ROLES

    return role if role in ROLES else "other"


def llm_provider_label(provider: Any) -> str:
    """The provider *kind* (``anthropic``, ``openai``, ...), never the user's config name."""
    from argus.providers.catalog import KINDS

    kind = getattr(provider, "kind", None)
    return kind if kind in KINDS else "other"


def llm_model_label(model: str | None) -> str:
    """Models in the pricing catalog keep their name (provider prefixes stripped); others are ``other``."""
    from argus.providers.pricing import get_price

    if not model or get_price(model) is None:
        return "other"
    return model.rsplit("/", 1)[-1][:80]


def finish_label(reason: str | None) -> str:
    return reason if reason in _FINISH_REASONS else "other"


def status_class(status: int | None) -> str:
    """``4xx`` / ``5xx`` for HTTP failures, ``network`` when the provider was never reached."""
    if status is None:
        return "network"
    return f"{status // 100}xx" if 100 <= status < 600 else "other"


def safe_label(value: str | None, limit: int = 80) -> str:
    """Bounds the *length* of a label value only. Prefer the ``*_label`` helpers above."""
    return (value or "unknown")[:limit]


# ------------------------------------------------------------ recording helpers
def record_llm_response(provider: Any, response: Any, seconds: float) -> None:
    """One completed provider call (streamed or not). Called by the LLM router only."""
    prov, mdl = llm_provider_label(provider), llm_model_label(response.model)
    LLM_CALLS.labels(prov, mdl, finish_label(response.finish_reason)).inc()
    LLM_TOKENS.labels(prov, mdl, "in").inc(response.usage.input_tokens)
    LLM_TOKENS.labels(prov, mdl, "out").inc(response.usage.output_tokens)
    LLM_COST.labels(prov, mdl).inc(response.usage.cost_usd)
    LLM_LATENCY.labels(prov).observe(seconds)


def record_llm_error(provider: Any, exc: Any) -> None:
    LLM_ERRORS.labels(
        llm_provider_label(provider),
        status_class(getattr(exc, "status", None)),
        "true" if getattr(exc, "retryable", False) else "false",
    ).inc()


def counts_failures[**P, T](
    counter: Counter,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator: increment ``counter`` whenever the wrapped coroutine raises (then re-raise)."""

    def wrap(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return await fn(*args, **kwargs)
            except Exception:
                counter.inc()
                raise

        return inner

    return wrap


async def tracked_stream[T](body: AsyncIterator[T], gauge: Gauge) -> AsyncIterator[T]:
    """Wrap a streaming response body so ``gauge`` counts it while it is open."""
    gauge.inc()
    try:
        async for chunk in body:
            yield chunk
    finally:
        gauge.dec()
        aclose = getattr(body, "aclose", None)
        if aclose is not None:
            await aclose()


def observes_webhook_delivery[**P](
    fn: Callable[P, Awaitable[bool]],
) -> Callable[P, Awaitable[bool]]:
    """Decorator for a delivery coroutine returning ``ok``: counts results and times the delivery."""

    @functools.wraps(fn)
    async def inner(*args: P.args, **kwargs: P.kwargs) -> bool:
        t0 = time.monotonic()
        result = "error"
        try:
            ok = await fn(*args, **kwargs)
            result = "ok" if ok else "failed"
            return ok
        finally:
            WEBHOOK_DELIVERIES.labels(result).inc()
            WEBHOOK_DELIVERY_SECONDS.observe(time.monotonic() - t0)

    return inner
