"""opencode ``run --format json`` (1.19.5).

One JSON object per line, ``{"type", "timestamp", "sessionID", ...}`` (the ``run`` handler):

* ``text`` ``{part}``: a whole text part, printed once when it is finished (``part.time.end``).
  There are no token deltas, so each part is emitted as one text event (deduplicated by part id).
* ``reasoning`` ``{part}``: a whole reasoning part (only with ``--thinking``, not passed today).
* ``step_start`` ``{part}`` and ``step_finish`` ``{part}``: the StepFinishPart carries ``cost``
  (USD, from the models.dev prices) and ``tokens`` ``{total, input, output, reasoning,
  cache: {read, write}}``. ``input`` excludes the cache reads and writes and ``output`` excludes
  reasoning, so reasoning is added to the output tokens. A turn can have several steps: summed.
* ``tool_use`` ``{part}``: a tool of opencode's own ran; the runner stops the turn on it.
* ``error`` ``{error}``: a NamedError ``{name, data}``: ``APIError`` (``message``, ``statusCode``,
  ``isRetryable``, ``responseBody``), ``ProviderAuthError`` (``providerID``, ``message``),
  ``ContextOverflowError``, ``MessageAbortedError``, ``MessageOutputLengthError``,
  ``UnknownError``. opencode retries 429/5xx itself before giving up, so an error that reaches
  the stream is final. Usage-plan exhaustion reads "Usage limit reached" (``FreeUsageLimitError``,
  ``GoUsageLimitError`` in the response body).

The process exits 1 after an error. A resumed session that no longer exists prints
"Session not found" on stderr and exits 1.
"""

from __future__ import annotations

from typing import Any

from argus.providers.cli.errors import Category, classify_status, classify_text
from argus.providers.cli.parsers import BaseParser, TurnResult, _int
from argus.providers.types import StreamEvent, Usage

_NAMES: dict[str, Category] = {
    "ProviderAuthError": "auth",
    "ContextOverflowError": "invalid",
    "MessageOutputLengthError": "error",
    "MessageAbortedError": "error",
}
_QUOTA_MARKERS = ("FreeUsageLimitError", "GoUsageLimitError", "insufficient_quota")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def classify(error: dict[str, Any]) -> tuple[Category, str]:
    name = str(error.get("name") or "")
    data = _dict(error.get("data"))
    message = str(data.get("message") or name or "opencode error")
    body = str(data.get("responseBody") or "")
    text = f"{message} {body}"
    if any(marker in text for marker in _QUOTA_MARKERS) or classify_text(message) == "quota":
        return "quota", message
    if name in _NAMES:
        return _NAMES[name], message
    if name == "APIError":
        status = data.get("statusCode")
        category = classify_status(status if isinstance(status, int) else None)
        if category is not None:
            return category, message
    return classify_text(text), message


class Parser(BaseParser):
    cli = "opencode"

    def __init__(self) -> None:
        super().__init__()
        self._seen_parts: set[str] = set()
        self._usage = Usage()
        self._cost: float | None = None

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        sid = event.get("sessionID")
        if isinstance(sid, str) and sid and not self.result.session_id:
            self.result.session_id = sid
        part = _dict(event.get("part"))
        if kind in ("text", "reasoning"):
            text = part.get("text")
            if not isinstance(text, str) or not text or part.get("synthetic") or part.get("ignored"):
                return []
            pid = str(part.get("id") or "")
            if pid:
                if pid in self._seen_parts:
                    return []
                self._seen_parts.add(pid)
            if kind == "reasoning":
                self._thinking_parts.append(text)
                return [StreamEvent(type="thinking", text=text)]
            chunk = ("\n\n" if self._text_parts else "") + text
            self._text_parts.append(chunk)
            return [StreamEvent(type="text", text=chunk)]
        if kind == "step_finish":
            tokens = _dict(part.get("tokens"))
            cache = _dict(tokens.get("cache"))
            self._usage = self._usage + Usage(
                input_tokens=_int(tokens.get("input")),
                output_tokens=_int(tokens.get("output")) + _int(tokens.get("reasoning")),
                cache_read_tokens=_int(cache.get("read")),
                cache_write_tokens=_int(cache.get("write")),
            )
            self.result.usage = self._usage
            cost = part.get("cost")
            if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
                self._cost = round((self._cost or 0.0) + float(cost), 8)
                self.result.reported_cost_usd = self._cost
            return []
        if kind == "error":
            error = _dict(event.get("error"))
            category, message = classify(error)
            self._fail(category, message)
            return []
        return []

    def finish(self, exit_record: dict[str, Any] | None) -> TurnResult:
        record = exit_record or {}
        if (
            record
            and not record.get("ok")
            and self.result.failure is None
            and "Session not found" in str(record.get("detail") or "")
        ):
            self._fail("session", "opencode: session not found")
        return super().finish(exit_record)
