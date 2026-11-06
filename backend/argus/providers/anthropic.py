"""Anthropic Claude provider, built on the official ``anthropic`` SDK."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from argus.core.logging import get_logger
from argus.providers.base import Provider, ProviderError
from argus.providers.pricing import estimate_cost
from argus.providers.types import (
    CompletionRequest,
    CompletionResponse,
    FinishReason,
    Message,
    StreamEvent,
    ToolCall,
    Usage,
)

# Models that accept the server-side refusal fallback ("default" routing form).
log = get_logger(__name__)

_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
_EPHEMERAL = {"type": "ephemeral"}

# Thinking on request (CompletionRequest.reasoning == "on"). Claude 4.6 and later take adaptive
# thinking; Opus 4.7 and later (and every 5.x model) return empty thinking text unless
# ``display: "summarized"`` is asked for, while 4.6 already summarizes (and predates ``display``).
# Models before 4.6 only take a fixed ``budget_tokens`` (at least 1024, below max_tokens); Claude 3
# models other than 3.7 Sonnet cannot think at all.
_LEGACY_THINKING = re.compile(
    r"claude-(?:3-7-sonnet|sonnet-3-7|(?:opus|sonnet|haiku)-4-(?:[0-5])(?!\d)|(?:opus|sonnet)-4(?:-20\d{6}|$|@))"
)
_NO_THINKING = re.compile(r"claude-(?:instant|2|3-(?![7])|(?:opus|sonnet|haiku)-3(?!-7))")
_SUMMARIZED_BY_DEFAULT = re.compile(r"claude-(?:opus|sonnet)-4-6(?!\d)")
MIN_THINKING_BUDGET = 1024
MAX_THINKING_BUDGET = 8_000

# Preserved thinking: on Claude 5-family models a thinking block is bound to the conversation prefix
# that produced it (top-level system prompt, tools, earlier messages). A council member's system
# prompt carries the live board and its history is compacted, so that prefix changes; accounts the
# API enforces (new accounts on Claude Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1) then get a 400
# "bound to a different conversation". Setting any ``prefix_mismatch_behavior`` opts a request into
# enforcement, so it is sent only after such a 400: from then on ``drop_block`` drops the stale
# blocks (and later ones) instead of failing, while unenforced accounts keep sending their blocks.
_BINDING_BETA = "thinking-binding-controls-2026-08-01"
_BOUND_TO_OTHER = "bound to a different conversation"
# Models that think when ``thinking`` is omitted (adaptive by default): Claude 5 family.
_THINKS_BY_DEFAULT = re.compile(r"^claude-(fable|mythos|opus|sonnet|haiku)-5(-|$)")


def _model_key(model: str) -> str:
    """The Claude model id without platform prefixes (``anthropic.``, ``us.anthropic.``, ``models/``)."""
    low = model.lower()
    i = low.find("claude-")
    return low[i:] if i >= 0 else low


def thinking_param(model: str, reasoning: str, max_tokens: int) -> dict[str, Any] | None:
    """The ``thinking`` request parameter for ``reasoning``, or None to leave it out.

    Only "on" sends anything: "auto" records whatever the model returns by default and "off" must
    not change the request either (current models reject ``{"type": "disabled"}``).
    """
    if reasoning != "on":
        return None
    key = _model_key(model)
    if _NO_THINKING.match(key):
        return None
    if _LEGACY_THINKING.match(key):
        budget = min(MAX_THINKING_BUDGET, max_tokens // 2)
        if budget < MIN_THINKING_BUDGET or budget >= max_tokens:
            return None
        return {"type": "enabled", "budget_tokens": budget}
    if _SUMMARIZED_BY_DEFAULT.match(key):
        return {"type": "adaptive"}
    return {"type": "adaptive", "display": "summarized"}


_STOP_MAP: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "refusal",
}


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages; consecutive tool results merge into one user turn."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content,
                "is_error": m.is_error,
            }
            if (
                out
                and out[-1]["role"] == "user"
                and isinstance(out[-1]["content"], list)
                and all(b.get("type") == "tool_result" for b in out[-1]["content"])
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if m.role == "assistant":
            if m.raw and m.raw.get("provider") == "anthropic":
                # Replay native blocks unchanged (thinking blocks must not be edited).
                out.append({"role": "assistant", "content": m.raw["content"]})
                continue
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            out.append({"role": "assistant", "content": blocks or m.content})
            continue
        out.append({"role": "user", "content": m.content})
    return out


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        name: str | None = None,
        refusal_fallback: bool = True,
        timeout: float = 600.0,
        client: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        super().__init__(name)
        self.refusal_fallback = refusal_fallback
        # Set once the API refused a stale thinking block for this account (see _BINDING_BETA).
        self.drop_mismatched_thinking = False
        # Credentials and endpoint are always explicit: otherwise the SDK falls back to ambient
        # ANTHROPIC_* env vars / credential profiles (e.g. a JWT-based proxy), so a user's
        # provider could silently talk to a different endpoint with a different credential type
        # for some calls. One client is shared by completions, streaming and model listing.
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key or "",
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            max_retries=2,
            # SSRF-guarded transport for user-configured endpoints (argus.core.netguard).
            http_client=http_client,
        )

    def _params(self, req: CompletionRequest, *, drop_mismatched: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": _to_anthropic_messages(req.messages),
        }
        blocks = req.system_blocks()
        cached = any(cacheable for _, cacheable in blocks)
        if cached:
            # Prompt caching: the stable prefix (persona, brief, guardrails, skills, standing orders)
            # carries a cache breakpoint; the volatile tail (board, guidance) comes after it.
            params["system"] = [
                {"type": "text", "text": text, **({"cache_control": _EPHEMERAL} if cacheable else {})}
                for text, cacheable in blocks
            ]
        elif req.system_prompt:
            params["system"] = req.system_prompt
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in req.tools
            ]
            if cached:
                # Tools come first in the cached prefix: a breakpoint on the last one keeps them
                # cached even when the system prefix changes (a new standing order).
                params["tools"][-1]["cache_control"] = _EPHEMERAL
        if req.stop:
            params["stop_sequences"] = req.stop
        if req.effort:
            params["output_config"] = {"effort": req.effort}
        thinking = thinking_param(req.model, req.reasoning, req.max_tokens)
        if thinking is not None:
            params["thinking"] = thinking
        # Sampling parameters are not sent: current Claude models reject them.
        if self.refusal_fallback and req.model in _FALLBACK_MODELS:
            params["betas"] = [_FALLBACK_BETA]
            params["fallbacks"] = "default"
        if drop_mismatched or self.drop_mismatched_thinking:
            self._bind_drop(params, req.model)
        return params

    @staticmethod
    def _bind_drop(params: dict[str, Any], model: str) -> None:
        """Ask the API to drop thinking blocks whose conversation prefix changed, not to fail."""
        if "thinking" not in params and not _THINKS_BY_DEFAULT.match(_model_key(model)):
            return  # no thinking on this request, so no block to bind (and none must be turned on)
        thinking = dict(params.get("thinking") or {"type": "adaptive"})
        thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
        params["thinking"] = thinking
        params["betas"] = [*params.get("betas", []), _BINDING_BETA]

    def _bound_to_other(self, exc: Exception, request: CompletionRequest) -> bool:
        """Whether ``exc`` is the preserved-thinking 400, and a retry with drop_block can fix it."""
        return (
            isinstance(exc, anthropic.APIStatusError)
            and exc.status_code == 400
            and _BOUND_TO_OTHER in str(getattr(exc, "message", "") or exc)
            and not self.drop_mismatched_thinking
            and bool(_THINKS_BY_DEFAULT.match(_model_key(request.model)) or request.reasoning == "on")
        )

    def _convert(self, msg: Any) -> CompletionResponse:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in msg.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "thinking":
                thinking_parts.append(getattr(block, "thinking", "") or "")
            elif btype == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        u = msg.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        usage.cost_usd = estimate_cost(msg.model, usage)
        raw_blocks = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in msg.content]
        return CompletionResponse(
            provider=self.name,
            model=msg.model,
            content="".join(text_parts),
            thinking="\n".join(t for t in thinking_parts if t),
            tool_calls=calls,
            finish_reason=_STOP_MAP.get(msg.stop_reason or "", "stop"),
            usage=usage,
            raw={"provider": "anthropic", "content": raw_blocks, "stop_reason": msg.stop_reason},
        )

    def _wrap(self, exc: Exception) -> ProviderError:
        if isinstance(exc, anthropic.APIStatusError):
            ra = exc.response.headers.get("retry-after") if exc.response is not None else None
            return ProviderError.from_status(
                self.name, exc.status_code, str(exc.message), float(ra) if ra and ra.isdigit() else None
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(f"{self.name}: connection error", provider=self.name, retryable=True)
        return ProviderError(f"{self.name}: {exc}", provider=self.name)

    def _api(self, params: dict[str, Any]) -> Any:
        return self.client.beta.messages if "betas" in params else self.client.messages

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        params = self._params(request)
        try:
            # Stream under the hood so long generations never hit HTTP timeouts.
            async with self._api(params).stream(**params) as stream:
                msg = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            if not self._bound_to_other(exc, request):
                raise self._wrap(exc) from exc
            self.drop_mismatched_thinking = True
            log.info("anthropic.thinking_binding_enforced", provider=self.name, model=request.model)
            return await self.complete(request)
        return self._convert(msg)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        params = self._params(request)
        yielded = False
        try:
            async with self._api(params).stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yielded = True
                            yield StreamEvent(type="text", text=delta.text)
                        elif delta.type == "thinking_delta":
                            yielded = True
                            yield StreamEvent(type="thinking", text=delta.thinking)
                msg = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            # The preserved-thinking 400 comes before any output, so a retry repeats nothing.
            if yielded or not self._bound_to_other(exc, request):
                raise self._wrap(exc) from exc
            self.drop_mismatched_thinking = True
            log.info("anthropic.thinking_binding_enforced", provider=self.name, model=request.model)
            async for event in self.stream(request):
                yield event
            return
        response = self._convert(msg)
        for call in response.tool_calls:
            yield StreamEvent(type="tool_call", tool_call=call)
        yield StreamEvent(type="done", response=response)

    async def list_models(self) -> list[str]:
        # Same client (endpoint + auth) as completions; auto-paginates.
        try:
            return [m.id async for m in self.client.models.list(limit=100)]
        except anthropic.AnthropicError as exc:
            raise self._wrap(exc) from exc

    async def aclose(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            await close()
