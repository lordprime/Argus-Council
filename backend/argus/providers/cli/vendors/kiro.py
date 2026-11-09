"""Kiro CLI over the Agent Client Protocol: the events ``argus/runner/acp.py`` yields.

* ``acp.session``: ``sessionId`` (a UUID Kiro assigned, or the one resumed), optional ``modelId``.
* ``acp.update``: a ``session/update`` notification, ``update.sessionUpdate`` one of
  ``agent_message_chunk`` (``content`` is a text block: streamed answer text), ``agent_thought_chunk``
  (reasoning), ``tool_call`` / ``tool_call_update`` (Kiro's tools are denied, so only a warning),
  ``plan``, ``usage_update``, ``available_commands_update``... (https://agentclientprotocol.com).
  Agent Client Protocol streams the answer only as chunks; there is no final message to de-duplicate against.
* ``acp.notification``: vendor notifications. ``_kiro.dev/error/rate_limit`` (CLI V2) and
  ``_kiro/error/rate_limit`` (V3) are Kiro's rate-limit notices.
* ``acp.result``: the ``session/prompt`` result, ``stopReason`` one of ``end_turn``, ``max_tokens``,
  ``max_turn_requests``, ``refusal``, ``cancelled`` (a content-filtered answer ends in ``refusal``).
* ``acp.error``: a JSON-RPC error (``reason`` ``rpc``, ``error.code``/``message``/``data``) or a
  local failure (``eof``, ``timeout``, ``protocol``, ``output_too_large``). ``-32000`` is the protocol's
  "authentication required" (Kiro answers every request with it while signed out).

Kiro documents no error codes for usage or rate limits; they are told apart by text (a 429
"Too many requests", "throttl...", a monthly usage or credit limit).
"""

from __future__ import annotations

import re
from typing import Any

from argus.providers.cli.errors import RUNNER_CATEGORIES, Category, classify_text
from argus.providers.cli.parsers import BaseParser, TurnResult, _int
from argus.providers.types import StreamEvent, Usage

AUTH_REQUIRED = -32000
_STOP_MESSAGES = {
    "max_tokens": "Kiro stopped: the answer reached the model's output token limit.",
    "max_turn_requests": "Kiro stopped: the turn reached its request limit.",
    "refusal": "Kiro refused to answer (the response was filtered).",
    "cancelled": "Kiro cancelled the turn.",
}
_QUOTA_RE = re.compile(
    r"usage limit|monthly limit|credit|quota|overage|subscription|billing|limit (?:has been |was )?reached|"
    r"reached (?:your|the) (?:\w+ )?limit",
    re.I,
)
_RATE_RE = re.compile(r"\b429\b|throttl|too many requests|rate.?limit|request limit|slow down", re.I)
_AUTH_RE = re.compile(
    r"auth(?:entication)? (?:is )?required|not (?:logged|signed) in|log ?in|sign ?in|unauthori[sz]ed|"
    r"\b40[13]\b|expired (?:token|session)|invalid (?:token|api key)|access ?denied|forbidden",
    re.I,
)


def classify_kiro(code: int | None, text: str) -> Category:
    if code == AUTH_REQUIRED:
        return "auth"
    if _QUOTA_RE.search(text):
        return "quota"
    if _RATE_RE.search(text):
        return "rate_limit"
    if _AUTH_RE.search(text):
        return "auth"
    if code in (-32602, -32600):
        return "invalid"
    return classify_text(text)


def _content_text(content: Any) -> str:
    blocks = content if isinstance(content, list) else [content]
    return "".join(
        b["text"]
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


class KiroParser(BaseParser):
    cli = "kiro"

    def __init__(self) -> None:
        super().__init__()
        self._result_seen = False
        self._closed_early = False
        self._rate_notice: str | None = None
        self._tool_calls: set[str] = set()

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if kind == "acp.session":
            if isinstance(event.get("sessionId"), str):
                self.result.session_id = event["sessionId"]
            if isinstance(event.get("modelId"), str):
                self.result.model = event["modelId"]
            return []
        if kind == "acp.update":
            return self._update(event.get("update") if isinstance(event.get("update"), dict) else {})
        if kind == "acp.notification":
            method = str(event.get("method") or "")
            if method.endswith("error/rate_limit"):
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                message = params.get("message") if isinstance(params.get("message"), str) else ""
                self._rate_notice = (message or "Kiro reported a rate limit.")[:300]
            return []
        if kind == "acp.result":
            self._result_seen = True
            self._usage(event.get("usage"))
            stop = str(event.get("stopReason") or "")
            if stop in _STOP_MESSAGES:
                self._fail("error", _STOP_MESSAGES[stop])
            return []
        if kind == "acp.error":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            if event.get("reason") == "eof":
                self._closed_early = True  # the exit record says why (stderr, exit code)
                return []
            code = err.get("code") if isinstance(err.get("code"), int) else None
            message = str(err.get("message") or "Kiro request failed")
            data = err.get("data")
            detail = f"{message}: {data}" if isinstance(data, str) and data else message
            if event.get("reason") == "timeout":
                self._fail("unavailable", "Kiro did not finish in time.")
            elif event.get("reason") == "output_too_large":
                self._fail("error", "Kiro's output exceeded the cap.")
            elif self._rate_notice and classify_kiro(code, detail) == "error":
                self._fail("rate_limit", f"{detail} ({self._rate_notice})")
            else:
                self._fail(classify_kiro(code, detail), detail)
        return []

    def _update(self, update: dict[str, Any]) -> list[StreamEvent]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                self._text_parts.append(text)
                return [StreamEvent(type="text", text=text)]
            return []
        if kind == "agent_thought_chunk":
            text = _content_text(update.get("content"))
            if text:
                self._thinking_parts.append(text)
                return [StreamEvent(type="thinking", text=text)]
            return []
        if kind == "tool_call":
            call_id = str(update.get("toolCallId") or "")
            if call_id not in self._tool_calls:
                self._tool_calls.add(call_id)
                title = str(update.get("title") or update.get("kind") or "a tool")[:100]
                self.result.warnings.append(
                    f"Kiro tried to use {title}; Argus refused it (its tools are off)."
                )
            return []
        if kind == "usage_update":
            cost = update.get("cost")
            if (
                isinstance(cost, dict)
                and cost.get("currency") == "USD"
                and isinstance(cost.get("amount"), int | float)
            ):
                self.result.reported_cost_usd = float(cost["amount"])
        return []

    def _usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        cache_read = _int(usage.get("cachedReadTokens", usage.get("cache_read_tokens")))
        self.result.usage = Usage(
            input_tokens=_int(usage.get("inputTokens", usage.get("input_tokens"))),
            output_tokens=_int(usage.get("outputTokens", usage.get("output_tokens"))),
            cache_read_tokens=cache_read,
            cache_write_tokens=_int(usage.get("cachedWriteTokens", usage.get("cache_write_tokens"))),
        )

    def finish(self, exit_record: dict[str, Any] | None) -> TurnResult:
        record = dict(exit_record) if exit_record else None
        if (
            record is not None
            and self._result_seen
            and not record.get("ok")
            and record.get("error") == "exit_status"
        ):
            # The prompt finished; the agent only exited untidily after the runner closed its stdin.
            record["ok"] = True
        if record is not None and record.get("ok") and not self._result_seen and self.result.failure is None:
            reason = "Kiro closed the connection before the turn finished." if self._closed_early else ""
            self._fail("unavailable", reason or "Kiro ended the turn without a result.")
        if self._rate_notice and self.result.failure is None and not (record or {}).get("ok", False):
            self._fail("rate_limit", self._rate_notice)
        if self._closed_early and self.result.failure is None and record is not None and not record.get("ok"):
            # Kiro died mid-turn: its stderr may say why (sign-in, limits); otherwise it is unavailable.
            detail = str(record.get("detail") or "").strip()
            category = RUNNER_CATEGORIES.get(str(record.get("error") or "")) or classify_kiro(None, detail)
            self._fail(
                "unavailable" if category == "error" else category,
                detail or "Kiro exited before the turn finished.",
            )
        result = super().finish(record)
        if self._rate_notice and result.failure is None:
            result.warnings.append(self._rate_notice)
        return result


Parser = KiroParser
