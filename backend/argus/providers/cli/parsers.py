"""Turn each CLI's JSON event stream into Argus stream events plus one turn result.

Event shapes follow each vendor's documented headless output:

* claude ``--output-format stream-json --verbose --include-partial-messages``
  (code.claude.com/docs/en/headless): ``system/init``, ``stream_event`` (Anthropic streaming
  events: ``content_block_delta`` with ``text_delta``/``thinking_delta``), ``assistant``,
  ``system/api_retry`` and a final ``result`` with ``usage``, ``total_cost_usd`` and ``session_id``.
* codex ``exec --json`` (developers.openai.com/codex, non-interactive mode): ``thread.started``
  (``thread_id``), ``turn.started``, ``item.started|updated|completed`` (``agent_message``,
  ``reasoning``, ``error``...), ``turn.completed`` (``usage``), ``turn.failed`` and ``error``.
* gemini ``--output-format stream-json`` (gemini-cli docs/cli/headless.md): ``init``
  (``session_id``, ``model``), ``message`` (``role``, ``content``, ``delta``), ``tool_use``,
  ``tool_result``, ``error`` and ``result`` (``status``, ``error``, ``stats``).

The runner appends one ``{"type": "argus.exit", ...}`` record when the process ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from argus.providers.cli.errors import (
    CLAUDE_CATEGORIES,
    RUNNER_CATEGORIES,
    Category,
    classify_status,
    classify_text,
)
from argus.providers.types import StreamEvent, Usage


@dataclass
class Failure:
    category: Category
    message: str


@dataclass
class TurnResult:
    text: str = ""
    thinking: str = ""
    usage: Usage = field(default_factory=Usage)
    reported_cost_usd: float | None = None  # what the CLI itself reports (claude: cumulative per session)
    session_id: str | None = None
    model: str | None = None
    auth_source: str | None = None
    failure: Failure | None = None
    warnings: list[str] = field(default_factory=list)


def _int(v: Any) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


class BaseParser:
    cli = "base"

    def __init__(self) -> None:
        self.result = TurnResult()
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self.exit: dict[str, Any] | None = None

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        raise NotImplementedError

    def _fail(self, category: Category, message: str) -> None:
        # Keep the first hard failure; later ones are usually consequences of it.
        if self.result.failure is None:
            self.result.failure = Failure(category, message.strip()[:500] or category)

    def finish(self, exit_record: dict[str, Any] | None) -> TurnResult:
        self.exit = exit_record or {
            "ok": False,
            "error": "stream_ended",
            "detail": "runner stream ended early",
        }
        if not self.result.text:
            self.result.text = "".join(self._text_parts)
        if not self.result.thinking:
            self.result.thinking = "".join(self._thinking_parts)
        if self.exit.get("session_id") and not self.result.session_id:
            self.result.session_id = str(self.exit["session_id"])
        if not self.exit.get("ok") and self.result.failure is None:
            code = str(self.exit.get("error") or "")
            detail = str(self.exit.get("detail") or code or "CLI failed")
            category = RUNNER_CATEGORIES.get(code) or classify_text(detail)
            self._fail(category, detail)
        return self.result


class ClaudeParser(BaseParser):
    cli = "claude"

    def __init__(self) -> None:
        super().__init__()
        self._streamed = False
        self._retry_category: Category | None = None

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if event.get("parent_tool_use_id"):
            return []  # subagent traffic; cannot happen with tools off, ignored defensively
        if kind == "system":
            sub = event.get("subtype")
            if sub == "init":
                self.result.session_id = event.get("session_id") or self.result.session_id
                self.result.model = event.get("model") or self.result.model
                self.result.auth_source = event.get("apiKeySource")
            elif sub == "api_retry":
                cat = CLAUDE_CATEGORIES.get(str(event.get("error") or ""))
                self._retry_category = cat or classify_status(event.get("error_status"))
            return []
        if kind == "stream_event":
            ev = event.get("event") or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self._streamed = True
                    self._text_parts.append(delta["text"])
                    return [StreamEvent(type="text", text=delta["text"])]
                if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    self._thinking_parts.append(delta["thinking"])
                    return [StreamEvent(type="thinking", text=delta["thinking"])]
            return []
        if kind == "assistant":
            message = event.get("message") or {}
            texts = [
                b.get("text", "")
                for b in message.get("content") or []
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if event.get("error") or event.get("is_api_error_message"):
                cat = CLAUDE_CATEGORIES.get(str(event.get("error") or "")) or classify_status(
                    event.get("api_error_status")
                )
                self._fail(cat or classify_text(" ".join(texts)), " ".join(texts) or str(event.get("error")))
                return []
            if message.get("model") and message.get("model") != "<synthetic>":
                self.result.model = message["model"]
            if not self._streamed and texts:
                joined = "".join(texts)
                self._text_parts.append(joined)
                return [StreamEvent(type="text", text=joined)]
            return []
        if kind == "result":
            self.result.session_id = event.get("session_id") or self.result.session_id
            usage = event.get("usage") or {}
            self.result.usage = Usage(
                input_tokens=_int(usage.get("input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
            )
            if isinstance(event.get("total_cost_usd"), int | float):
                self.result.reported_cost_usd = float(event["total_cost_usd"])
            text = event.get("result")
            if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
                cat = classify_status(event.get("api_error_status")) or self._retry_category
                message = text if isinstance(text, str) and text else str(event.get("subtype") or "error")
                self._fail(cat or classify_text(message), message)
            elif isinstance(text, str):
                self.result.text = text
        return []


class CodexParser(BaseParser):
    cli = "codex"

    def __init__(self) -> None:
        super().__init__()
        self._last_error = ""

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if kind == "thread.started":
            self.result.session_id = event.get("thread_id") or self.result.session_id
            return []
        if kind == "item.completed":
            item = event.get("item") or {}
            itype = item.get("type")
            text = item.get("text") if isinstance(item.get("text"), str) else ""
            if itype == "agent_message" and text:
                sep = "\n\n" if self._text_parts else ""
                self._text_parts.append(sep + text)
                return [StreamEvent(type="text", text=sep + text)]
            if itype == "reasoning" and text:
                self._thinking_parts.append(text)
                return [StreamEvent(type="thinking", text=text)]
            if itype == "error" and isinstance(item.get("message"), str):
                self.result.warnings.append(item["message"][:300])
            return []
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            cached = _int(usage.get("cached_input_tokens"))
            self.result.usage = Usage(
                input_tokens=max(0, _int(usage.get("input_tokens")) - cached),
                output_tokens=_int(usage.get("output_tokens")),
                cache_read_tokens=cached,
            )
            return []
        if kind == "turn.failed":
            message = str((event.get("error") or {}).get("message") or self._last_error or "turn failed")
            self._fail(classify_text(message), message)
            return []
        if kind == "error":
            self._last_error = str(event.get("message") or "")
        return []


class GeminiParser(BaseParser):
    cli = "gemini"

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if kind == "init":
            self.result.session_id = event.get("session_id") or self.result.session_id
            self.result.model = event.get("model") or self.result.model
            return []
        if kind == "message":
            if (
                event.get("role") == "assistant"
                and isinstance(event.get("content"), str)
                and event["content"]
            ):
                self._text_parts.append(event["content"])
                return [StreamEvent(type="text", text=event["content"])]
            return []
        if kind == "error":
            message = str(event.get("message") or "")
            if event.get("severity") == "error":
                self.result.warnings.append(message[:300])
            return []
        if kind == "result":
            stats = event.get("stats") or {}
            cached = _int(stats.get("cached"))
            self.result.usage = Usage(
                input_tokens=max(0, _int(stats.get("input_tokens")) - cached),
                output_tokens=_int(stats.get("output_tokens")),
                cache_read_tokens=cached,
            )
            models = stats.get("models")
            if isinstance(models, dict) and len(models) == 1:
                self.result.model = next(iter(models))
            if event.get("status") == "error":
                err = event.get("error") or {}
                message = str(err.get("message") or "Gemini CLI error")
                etype = str(err.get("type") or "")
                category: Category = "auth" if "Auth" in etype else classify_text(message)
                self._fail(category, message)
        return []


def make_parser(cli: str) -> BaseParser:
    return {"claude": ClaudeParser, "codex": CodexParser, "gemini": GeminiParser}[cli]()
