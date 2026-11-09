"""Goose ``run --output-format stream-json`` (v1.57.0, crates/goose-cli/src/session/mod.rs).

One JSON object per line, tagged ``type`` in snake case:

* ``message``: ``{"message": {"id", "role", "created", "content": [...], "metadata": {...}}}``.
  Streamed assistant output comes as one ``message`` per chunk; chunks of the same reply share the
  message ``id`` and each carries only its new text (crates/goose/src/agents/reply_parts.rs), so a
  chunk's ``text`` blocks are deltas. Content blocks are tagged ``type`` in camel case: ``text``,
  ``thinking`` (``thinking``), ``redactedThinking``, ``toolRequest``, ``systemNotification``
  (``notificationType``, ``msg``) and ``error`` (``kind``: ``authentication`` /
  ``contextLengthExceeded`` / ``creditsExhausted`` / ``other``, ``message``).
* ``notification``: extension logs and progress (none with ``--no-profile``).
* ``error``: ``{"error": "<string>"}``, the turn's terminal error (exit code 1).
* ``complete``: ``total_tokens`` and, when known, ``input_tokens``, ``output_tokens``,
  ``cache_read_input_tokens``, ``cache_write_input_tokens``, ``cost_usd``. These are the session's
  running totals; ``input_tokens`` includes the cache reads and writes.

Provider errors read ``"Rate limit exceeded: ..."``, ``"Credits exhausted: ..."`` and
``"Authentication error: ..."`` (crates/goose-provider-types/src/errors.rs). In a message's
``error`` block they are wrapped ("Ran into this error: Rate limit exceeded: ..."), so the prefixes
are matched anywhere in the string. Credits exhausted arrives as a ``creditsExhausted`` system
notification instead of an ``error`` block, and the run can still end with ``complete``.

The session id never appears in the stream: the runner keeps the one Argus chose (``-n <uuid>``).
"""

from __future__ import annotations

from typing import Any

from argus.providers.cli.errors import Category, classify_text
from argus.providers.cli.parsers import BaseParser, _int
from argus.providers.types import StreamEvent, Usage

# ProviderError messages (errors.rs), most specific first.
_PREFIXES: tuple[tuple[str, Category], ...] = (
    ("Authentication error:", "auth"),
    ("Credits exhausted:", "quota"),
    ("Rate limit exceeded:", "rate_limit"),
    ("Provider is not configured", "auth"),
    ("Context length exceeded:", "invalid"),
    ("Server error:", "unavailable"),
    ("Network error:", "unavailable"),
)
_KINDS: dict[str, Category] = {
    "authentication": "auth",
    "creditsExhausted": "quota",
    "contextLengthExceeded": "invalid",
}
# Written by Goose itself as an assistant message when --max-turns is reached (ops_maxturns.rs).
MAX_TURNS_MESSAGE = (
    "I've reached the maximum number of actions I can do without user input. Would you like me to continue?"
)


def classify(message: str) -> Category:
    for prefix, category in _PREFIXES:
        if prefix in message:
            return category
    return classify_text(message)


class Parser(BaseParser):
    cli = "goose"

    def __init__(self) -> None:
        super().__init__()
        self._by_id: dict[str, list[str]] = {}  # assistant message id -> its text chunks

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if kind == "message":
            return self._message(event.get("message"))
        if kind == "error":
            message = str(event.get("error") or "goose failed")
            self._fail(classify(message), message)
            return []
        if kind == "complete":
            inp = _int(event.get("input_tokens"))
            read = _int(event.get("cache_read_input_tokens"))
            write = _int(event.get("cache_write_input_tokens"))
            self.result.usage = Usage(
                input_tokens=max(0, inp - read - write),
                output_tokens=_int(event.get("output_tokens")),
                cache_read_tokens=read,
                cache_write_tokens=write,
            )
            cost = event.get("cost_usd")
            if isinstance(cost, int | float) and not isinstance(cost, bool):
                self.result.reported_cost_usd = float(cost)
        return []

    def _message(self, message: Any) -> list[StreamEvent]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []  # the user's own prompt, echoed back
        inference = (message.get("metadata") or {}).get("inference")
        if isinstance(inference, dict):
            model = inference.get("resolvedModel") or inference.get("requestedModel")
            if isinstance(model, str) and model:
                self.result.model = model
        msg_id = str(message.get("id") or "")
        blocks = [b for b in message.get("content") or [] if isinstance(b, dict)]
        out: list[StreamEvent] = []
        texts: list[str] = []
        for block in blocks:
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif btype == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"]:
                self._thinking_parts.append(block["thinking"])
                out.append(StreamEvent(type="thinking", text=block["thinking"]))
            elif btype == "error":
                text = str(block.get("message") or "")
                category = _KINDS.get(str(block.get("kind") or "")) or classify(text)
                self._fail(category, text or category)
            elif btype == "systemNotification" and block.get("notificationType") == "creditsExhausted":
                self._fail(
                    "quota", "Credits exhausted: " + str(block.get("msg") or "add credits with the provider")
                )
        text = "".join(texts)
        if not text:
            return out
        chunks = self._by_id.setdefault(msg_id, [])
        if not chunks and text.strip() == MAX_TURNS_MESSAGE:
            self.result.warnings.append("goose stopped at its turn limit")
            return out
        if len(chunks) > 1 and text == "".join(chunks):
            return out  # the whole reply again under its id (Goose 1.57 does not, but never twice)
        # Each chunk carries only its new text, so chunks are appended as they come. No separator is
        # added between message ids: an OpenAI-compatible server that changes the id per chunk would
        # otherwise split words, and with tools off a turn is a single reply.
        chunks.append(text)
        self._text_parts.append(text)
        out.append(StreamEvent(type="text", text=text))
        return out
