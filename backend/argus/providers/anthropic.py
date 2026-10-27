"""Anthropic Claude provider, built on the official ``anthropic`` SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

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
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"

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

    def _params(self, req: CompletionRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": _to_anthropic_messages(req.messages),
        }
        if req.system_prompt:
            params["system"] = req.system_prompt
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in req.tools
            ]
        if req.stop:
            params["stop_sequences"] = req.stop
        if req.effort:
            params["output_config"] = {"effort": req.effort}
        # Sampling parameters are not sent: current Claude models reject them.
        if self.refusal_fallback and req.model in _FALLBACK_MODELS:
            params["betas"] = [_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

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
            raise self._wrap(exc) from exc
        return self._convert(msg)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        params = self._params(request)
        try:
            async with self._api(params).stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield StreamEvent(type="text", text=delta.text)
                        elif delta.type == "thinking_delta":
                            yield StreamEvent(type="thinking", text=delta.thinking)
                msg = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise self._wrap(exc) from exc
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
