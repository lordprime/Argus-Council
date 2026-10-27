"""Prometheus metrics for the API and the council runtime."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

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
PROTOCOL_CALLS = Counter(
    "argus_protocol_calls_total", "MCP/A2A/ACP calls", ["protocol", "direction", "status"], registry=REGISTRY
)


def safe_label(value: str | None, limit: int = 80) -> str:
    """Bound label cardinality/size (labels come partly from user-configured names)."""
    return (value or "unknown")[:limit]


# LLM failure modes (recorded by the router, so agents, overseers and /llm/complete all count).
LLM_ERRORS = Counter(
    "argus_llm_errors_total",
    "LLM provider errors",
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
