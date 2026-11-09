"""Qwen Code 0.27.1 ``--output-format stream-json --include-partial-messages``.

The stream is Claude-shaped, so this reuses :class:`ClaudeParser`: ``system/init`` (``session_id``,
``model``, ``tools``), ``stream_event`` (``content_block_delta`` with ``text_delta`` /
``thinking_delta``), ``assistant`` and a final ``result``. The differences handled here:

* ``result.usage.input_tokens`` is Gemini's ``promptTokenCount``, which includes the cached tokens
  also reported as ``cache_read_input_tokens``; Argus counts them once.
* An error result carries ``error.message`` (no ``result`` text, no ``api_error_status``) and no
  cost is reported.
* Exit codes of its ``Fatal*Error`` classes: 41 authentication, 42 bad input, 52 config, 53 turn
  limit, 54 tool execution, 55 budget (with ``--max-tool-calls 0``: the model asked for a tool).
  53 and 55 are Argus's own limits, not the provider's, so they do not trigger a fallback.
"""

from __future__ import annotations

import re
from typing import Any

from argus.providers.cli.errors import Category, classify_status, classify_text
from argus.providers.cli.parsers import ClaudeParser, TurnResult, _int
from argus.providers.types import StreamEvent, Usage

EXIT_CATEGORIES: dict[int, tuple[Category, str]] = {
    41: ("auth", "Qwen Code could not authenticate. Check the API key and its provider."),
    42: ("invalid", "Qwen Code rejected its input."),
    52: ("error", "Qwen Code configuration error."),
    53: ("error", "Qwen Code stopped at its turn limit."),
    54: ("error", "Qwen Code tried to run a tool (tools are off for council seats)."),
    55: ("error", "Qwen Code stopped: the model asked for a tool, and tools are off for council seats."),
}

# Provider messages the generic patterns miss. DashScope: "Arrearage" (account overdue) and
# "Throttling.AllocationQuota"; the discontinued Qwen OAuth free tier.
_QWEN_PATTERNS: list[tuple[re.Pattern[str], Category]] = [
    (re.compile(r"arrearage|good standing|allocationquota|free allocated quota", re.I), "quota"),
    (re.compile(r"free tier was discontinued|run /auth|credentials are no longer valid", re.I), "auth"),
    (re.compile(r"throttling", re.I), "rate_limit"),
]


def classify(text: str) -> Category:
    for pattern, category in _QWEN_PATTERNS:
        if pattern.search(text or ""):
            return category
    return classify_text(text)


class Parser(ClaudeParser):
    cli = "qwen"

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        if event.get("type") != "result":
            return super().feed(event)
        self.result.session_id = event.get("session_id") or self.result.session_id
        usage = event.get("usage") or {}
        cached = _int(usage.get("cache_read_input_tokens"))
        self.result.usage = Usage(
            input_tokens=max(0, _int(usage.get("input_tokens")) - cached),
            output_tokens=_int(usage.get("output_tokens")),
            cache_read_tokens=cached,
            cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
        )
        if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
            err = event.get("error")
            message = ""
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                message = err["message"]
            elif isinstance(event.get("result"), str):
                message = event["result"]
            message = message or str(event.get("subtype") or "error")
            status = event.get("api_error_status")
            category = classify_status(status if isinstance(status, int) else None) or self._retry_category
            self._fail(category or classify(message), message)
        elif isinstance(event.get("result"), str):
            self.result.text = event["result"]
        return []

    def finish(self, exit_record: dict[str, Any] | None) -> TurnResult:
        # Only a plain non-zero exit is the CLI's own verdict; timeouts, output caps and the
        # tools-not-disabled kill keep the runner's categories (BaseParser.finish).
        if exit_record and not exit_record.get("ok") and exit_record.get("error") == "exit_status":
            code = exit_record.get("exit_code")
            detail = str(exit_record.get("detail") or "").strip()
            mapped = EXIT_CATEGORIES.get(code) if isinstance(code, int) else None
            if mapped is not None:
                category, message = mapped
                if self.result.failure is None:
                    # stderr may name a more specific cause (a 429 or a quota message).
                    better = classify(detail) if detail else "error"
                    if category == "error" and better in ("quota", "rate_limit"):
                        category = better
                    self._fail(category, f"{message} {detail[:300]}".strip())
                elif code == 41 and self.result.failure.category == "error":
                    self.result.failure.category = "auth"
            elif self.result.failure is None and detail:
                self._fail(classify(detail), detail)
        return super().finish(exit_record)
