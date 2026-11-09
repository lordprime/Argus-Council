"""Mistral Vibe 3.1.0 headless events (``vibe -p --output streaming --output-dir D``).

stdout carries completed history entries, one JSON object per line (``headless_output.rs``):
``{"id", "turnId", "type": "message"|"reasoning"|"effect"|"callback"|"checkpoint"|"notice",
"generationStatus": "completed", "role", "content": [{"type": "text", "text"}...], "outcome"}``.
There are no token deltas, so each assistant message's text is emitted once, when it completes.
A draft a hook discarded (``outcome.type == "discarded"``) is never part of the answer.

The session id, usage, cost and the error are only in ``D/export.json``, which the runner reads
after exit and passes on as ``{"type": "argus.result_file", "data": <export>}``
(``run_export/model.rs``): ``outcome``, ``error: {message, code}``, ``session_id``,
``usage: {input_tokens, output_tokens, cached_input_tokens, total_tokens}``, ``cost_usd``.
"""

from __future__ import annotations

import re
from typing import Any

from argus.providers.cli.errors import Category, classify_text
from argus.providers.cli.parsers import BaseParser, _int
from argus.providers.types import StreamEvent, Usage

# export.json error.code: the app server's TurnErrorCode values plus the RPC ``unauthorized``.
CODE_CATEGORIES: dict[str, Category] = {
    "rate_limit": "rate_limit",
    "unauthorized": "auth",
    "invalid_api_key": "auth",
    "backend_error": "unavailable",
    "incomplete_stream": "unavailable",
    "internal_error": "unavailable",
    "invalid_model": "invalid",
    "context_too_long": "invalid",
    "invalid_image_attachment": "invalid",
    "images_not_supported": "invalid",
    "response_too_long": "error",
    "compaction_failed": "error",
    "refusal": "error",
}

# export.json outcome, when the error carries no code. Turn, token and price limits are budgets
# running out; a deadline or termination is the run being cut short.
OUTCOME_CATEGORIES: dict[str, Category] = {
    "turn_limit": "quota",
    "token_limit": "quota",
    "price_limit": "quota",
    "deadline": "unavailable",
    "terminated": "unavailable",
    "aborted": "unavailable",
    "infrastructure_failure": "unavailable",
    "length": "invalid",
    "usage_error": "invalid",
    "config_error": "invalid",
    "refusal": "error",
}

# A session that cannot open for want of a key: "Missing MISTRAL_API_KEY environment variable for
# mistral provider" (config_error, no code), or the keyring variant of it.
_MISSING_KEY = re.compile(r"\bMissing \w*API_KEY\b|\bAPI_KEY\b.*\bkeyring\b|\bkeyring\b.*\bAPI_KEY\b", re.I)


class Parser(BaseParser):
    cli = "vibe"

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = event.get("type")
        if kind == "argus.result_file":
            data = event.get("data")
            if isinstance(data, dict):
                self._export(data)
            return []
        if isinstance(kind, str) and kind.startswith("argus."):
            return []
        if event.get("generationStatus", "completed") != "completed":
            return []
        entry_id = event.get("id")
        if isinstance(entry_id, str):
            if entry_id in self._seen:
                return []  # the same entry printed twice: never duplicate text
            self._seen.add(entry_id)
        outcome = event.get("outcome")
        if isinstance(outcome, dict) and outcome.get("type") == "discarded":
            return []
        if kind == "message":
            if event.get("role") != "assistant":
                return []
            texts = [
                b["text"]
                for b in event.get("content") or []
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
            ]
            text = "\n\n".join(t for t in texts if t)
            if not text:
                return []
            sep = "\n\n" if self._text_parts else ""
            self._text_parts.append(sep + text)
            return [StreamEvent(type="text", text=sep + text)]
        if kind == "reasoning":
            text = event.get("text")
            if isinstance(text, str) and text:
                self._thinking_parts.append(text)
                return [StreamEvent(type="thinking", text=text)]
            return []
        if kind == "notice":
            if event.get("level") in ("warning", "error") and isinstance(event.get("message"), str):
                self.result.warnings.append(event["message"][:300])
        return []

    def _export(self, data: dict[str, Any]) -> None:
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            self.result.session_id = sid
        usage = data.get("usage")
        if isinstance(usage, dict):
            cached = _int(usage.get("cached_input_tokens"))
            self.result.usage = Usage(
                input_tokens=max(0, _int(usage.get("input_tokens")) - cached),
                output_tokens=_int(usage.get("output_tokens")),
                cache_read_tokens=cached,
            )
        cost = data.get("cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
            self.result.reported_cost_usd = float(cost)
        config = data.get("config")
        if (
            isinstance(config, dict)
            and isinstance(config.get("active_model"), str)
            and config["active_model"]
        ):
            self.result.model = config["active_model"]
        for warning in data.get("warnings") or []:
            if isinstance(warning, dict) and isinstance(warning.get("message"), str):
                self.result.warnings.append(warning["message"][:300])
        outcome = str(data.get("outcome") or "")
        if outcome in ("", "finished"):
            return
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "") or f"Vibe run ended: {outcome}"
        self._fail(_category(code, outcome, message), message)


def _category(code: str, outcome: str, message: str) -> Category:
    text = classify_text(message)
    by_code = CODE_CATEGORIES.get(code)
    if by_code is not None:
        # A plan key's monthly Vibe budget running out is not a documented code: it arrives as a
        # rate limit or backend error whose text says so.
        if by_code in ("rate_limit", "unavailable") and text == "quota":
            return "quota"
        return by_code
    if _MISSING_KEY.search(message):
        return "auth"
    if outcome in ("usage_error", "config_error", "infrastructure_failure") and text != "error":
        return text
    return OUTCOME_CATEGORIES.get(outcome) or text
