"""GitHub Copilot CLI 1.0.108 events (``copilot --output-format json --stream on``).

One JSON object per line, ``{id, timestamp, parentId, ephemeral, agentId, type, data}`` (the package's
``schemas/session-events.schema.json``; the stream is not documented for prompt mode, so unknown
fields and events are ignored):

* ``session.start``: ``data.sessionId``, ``data.selectedModel`` (``session.resume`` on a resume);
* ``assistant.message_delta``: ``data.messageId``, ``data.deltaContent``; ``assistant.message``:
  ``data.messageId``, ``data.content``, ``data.model``, ``data.reasoningText`` (the full text of a
  message whose deltas were already streamed is not emitted again);
* ``assistant.reasoning_delta`` / ``assistant.reasoning``: thinking;
* ``session.error``: ``data.errorType``, ``errorCode``, ``statusCode``, ``message``;
  ``model.call_failure``: ``data.statusCode``, ``errorCode``, ``errorMessage`` (a call may be
  retried, so it only decides the category when the turn then fails);
* ``session.warning``: ``data.message``; a final ``{"type": "result", "sessionId", "exitCode",
  "usage": {...}}`` (premium requests and durations, no tokens).

Token usage is not on stdout. ``--usage-output-file`` writes the session's usage (the API's
``UsageGetMetricsResult``: ``modelMetrics.<model>.usage.{inputTokens, outputTokens,
cacheReadTokens, cacheWriteTokens}``, ``tokenDetails``, ``totalNanoAiu``, ``currentModel``), which
the runner reads after exit and passes on as ``{"type": "argus.result_file", "data": ...}``. The
figures are session totals (the adapter takes the difference from the seat's previous turn) and
``inputTokens`` includes the cached tokens. Copilot bills in AI credits / premium requests, not
dollars, so no cost is reported.
"""

from __future__ import annotations

import re
from typing import Any

from argus.providers.cli.errors import Category, classify_status, classify_text
from argus.providers.cli.parsers import BaseParser, TurnResult, _int
from argus.providers.types import StreamEvent, Usage

# Error codes seen in the 1.0.108 runtime (Copilot API error codes and its own limit codes).
QUOTA_CODES: frozenset[str] = frozenset(
    {
        "quota_exceeded",
        "custom_quota_exceeded",
        "free_quota_exceeded",
        "session_quota_exceeded",
        "overage_limit_reached",
        "additional_spend_limit_reached",
        "billing_not_configured",
        "quota",
    }
)
RATE_CODES: frozenset[str] = frozenset(
    {
        "rate_limited",
        "rate_limit",
        "user_global_rate_limited",
        "user_weekly_rate_limited",
        "user_model_rate_limited",
        "integration_rate_limited",
    }
)
AUTH_CODES: frozenset[str] = frozenset({"authentication", "auth", "unauthorized", "forbidden"})

# "Plan exhausted": the service's quota message ("Plan: 100% used, resets ..."), AI credits or
# premium requests running out, a spending limit.
_QUOTA_RE = re.compile(
    r"quota|plan\b.{0,40}\b(?:exhausted|used up|100% used|limit)|premium requests?\b.{0,40}"
    r"\b(?:exhausted|limit|used)|ai credits?\b.{0,40}\b(?:exhausted|limit|used)|"
    r"spend(?:ing)? limit|overage|billing|usage limit",
    re.I,
)
# "You've hit the rate limit for this model." / "...your session rate limit." / "...weekly rate
# limit." / "You've hit your rate limit." (`copilot help limits`).
_RATE_RE = re.compile(r"rate.?limit|too many requests|\b429\b", re.I)
# "Error: No authentication information found." / "Authentication token found but could not be
# validated." / classic ghp_ PATs refused / no Copilot access on the account.
_AUTH_RE = re.compile(
    r"authenticat|not authori[sz]ed|unauthori[sz]ed|\b401\b|\b403\b|\blog ?in\b|\bsign ?in\b|"
    r"personal access token|\bghp_|copilot (?:is not enabled|access)|no copilot|credential",
    re.I,
)


def _category(status: Any, codes: list[str], message: str) -> Category:
    lowered = {c.lower() for c in codes if c}
    status_i = status if isinstance(status, int) and not isinstance(status, bool) else None
    if lowered & QUOTA_CODES or _QUOTA_RE.search(message):
        return "quota"
    if lowered & RATE_CODES or status_i == 429 or _RATE_RE.search(message):
        return "rate_limit"
    if lowered & AUTH_CODES or status_i in (401, 403) or _AUTH_RE.search(message):
        return "auth"
    return classify_status(status_i) or classify_text(message)


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


class Parser(BaseParser):
    cli = "copilot"

    def __init__(self) -> None:
        super().__init__()
        self._streamed: set[str] = set()  # message ids whose deltas were emitted
        self._reasoned: set[str] = set()  # reasoning ids whose deltas were emitted
        self._message: str | None = None  # message id the text currently belongs to
        self._call_failure: tuple[Category, str] | None = None

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if kind == "argus.result_file":
            data = event.get("data")
            if isinstance(data, dict):
                self._usage(data)
            return []
        if not isinstance(kind, str) or kind.startswith("argus."):
            return []
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event.get("agentId") or data.get("parentToolCallId"):
            return []  # subagent traffic; cannot happen with tools off, ignored defensively
        if kind == "session.start":
            sid = _str(data.get("sessionId"))
            self.result.session_id = sid or self.result.session_id
            self.result.model = _str(data.get("selectedModel")) or self.result.model
            return []
        if kind == "session.resume":
            self.result.model = _str(data.get("selectedModel")) or self.result.model
            return []
        if kind == "assistant.message_delta":
            delta = _str(data.get("deltaContent"))
            if not delta:
                return []
            mid = _str(data.get("messageId"))
            self._streamed.add(mid)
            return self._text(mid, delta)
        if kind == "assistant.message":
            self.result.model = _str(data.get("model")) or self.result.model
            mid = _str(data.get("messageId"))
            out: list[StreamEvent] = []
            reasoning = _str(data.get("reasoningText"))
            if reasoning and not self._thinking_parts:
                self._thinking_parts.append(reasoning)
                out.append(StreamEvent(type="thinking", text=reasoning))
            content = _str(data.get("content"))
            if content and (not mid or mid not in self._streamed):
                out += self._text(mid or None, content)
            return out
        if kind == "assistant.reasoning_delta":
            delta = _str(data.get("deltaContent"))
            if not delta:
                return []
            self._reasoned.add(_str(data.get("reasoningId")))
            self._thinking_parts.append(delta)
            return [StreamEvent(type="thinking", text=delta)]
        if kind == "assistant.reasoning":
            content = _str(data.get("content"))
            if content and _str(data.get("reasoningId")) not in self._reasoned:
                self._thinking_parts.append(content)
                return [StreamEvent(type="thinking", text=content)]
            return []
        if kind == "session.error":
            message = _str(data.get("message")) or _str(data.get("errorType")) or "Copilot CLI error"
            codes = [_str(data.get(k)) for k in ("errorCode", "errorType", "providerErrorType")]
            category = _category(data.get("statusCode"), codes, message)
            if category == "error" and self._call_failure is not None:
                category = self._call_failure[0]
            self._fail(category, message)
            return []
        if kind == "model.call_failure":
            message = _str(data.get("errorMessage")) or _str(data.get("failureKind")) or "model call failed"
            codes = [_str(data.get(k)) for k in ("errorCode", "errorType", "failureKind")]
            self._call_failure = (_category(data.get("statusCode"), codes, message), message)
            return []
        if kind == "session.warning":
            message = _str(data.get("message"))
            if message:
                self.result.warnings.append(message[:300])
            return []
        if kind == "result":
            sid = _str(event.get("sessionId"))
            self.result.session_id = sid or self.result.session_id
        return []

    def _text(self, mid: str | None, text: str) -> list[StreamEvent]:
        # A new assistant message (another chunk or turn) is separated from the previous one.
        sep = "\n\n" if self._text_parts and (mid is None or mid != self._message) else ""
        self._message = mid
        self._text_parts.append(sep + text)
        return [StreamEvent(type="text", text=sep + text)]

    def _usage(self, data: dict[str, Any]) -> None:
        inp = out = cache_read = cache_write = 0
        metrics = data.get("modelMetrics")
        found = False
        if isinstance(metrics, dict):
            for metric in metrics.values():
                usage = metric.get("usage") if isinstance(metric, dict) else None
                if not isinstance(usage, dict):
                    continue
                found = True
                inp += _int(usage.get("inputTokens"))
                out += _int(usage.get("outputTokens"))
                cache_read += _int(usage.get("cacheReadTokens"))
                cache_write += _int(usage.get("cacheWriteTokens"))
            # inputTokens counts the tokens read from and written to the prompt cache too.
            inp = max(0, inp - cache_read - cache_write)
        details = data.get("tokenDetails")
        if not found and isinstance(details, dict):
            # Session-wide per-type counts; "input" is already the uncached part.
            def count(name: str) -> int:
                entry = details.get(name)
                return _int(entry.get("tokenCount")) if isinstance(entry, dict) else 0

            found = True
            inp, out = count("input"), count("output")
            cache_read = count("cache_read")
            cache_write = count("cache_write") + count("cache_write_1h")
        if found:
            self.result.usage = Usage(
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
        model = _str(data.get("currentModel"))
        if model:
            self.result.model = model

    def finish(self, exit_record: dict[str, Any] | None) -> TurnResult:
        # A failed model call decides the category when the CLI then exits with an error of its own
        # (not when Argus stopped it: timeout, output cap, tools found).
        if (
            self.result.failure is None
            and self._call_failure is not None
            and exit_record is not None
            and not exit_record.get("ok")
            and exit_record.get("error") in (None, "exit_status")
        ):
            self._fail(*self._call_failure)
        return super().finish(exit_record)
