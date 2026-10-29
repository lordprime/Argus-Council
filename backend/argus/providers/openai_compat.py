"""OpenAI Chat Completions-compatible provider.

Covers OpenAI itself plus the many backends that speak the same wire format:
Mistral, Groq, OpenRouter, DeepSeek, xAI, Together, and self-hosted servers
such as vLLM, LM Studio, llama.cpp and Ollama (``/v1``).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

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

_FINISH: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "refusal",
}


def _to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in m.tool_calls
                ]
            out.append(msg)
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw, "_error": "invalid_json"}
    return val if isinstance(val, dict) else {"value": val}


# OpenAI reasoning models (o1, o3, o4-mini, gpt-5...) take only the default temperature.
_REASONING_MODEL = re.compile(r"^(o\d|gpt-5)")
# Parameters an endpoint may reject with a 400 "unsupported parameter/value", and how to adapt.
_MAX_TOKENS = "max_completion_tokens"  # quirk: send this instead of max_tokens
_NO_TEMPERATURE = "no_temperature"  # quirk: leave temperature out


def _quirk_for(status: int, text: str) -> str | None:
    """The request change a 400 asks for, if it is one of the known parameter mismatches."""
    if status != 400:
        return None
    low = text.lower()
    if "max_tokens" in low and ("max_completion_tokens" in low or "unsupported" in low):
        return _MAX_TOKENS
    if "temperature" in low and (
        "unsupported" in low or "does not support" in low or "only the default" in low
    ):
        return _NO_TEMPERATURE
    return None


class _Rejected(Exception):
    """A streaming request the endpoint refused before sending anything."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(str(error))
        self.error = error


class OpenAICompatProvider(Provider):
    kind = "openai"

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        name: str | None = None,
        kind: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if kind:
            self.kind = kind
        super().__init__(name)
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        self.headers = headers
        # Per model: request changes an endpoint asked for (see _quirk_for), so each is paid for once.
        self._quirks: dict[str, set[str]] = {}

    def _quirks_for(self, model: str) -> set[str]:
        quirks = set(self._quirks.get(model, ()))
        if self.kind == "openai":
            # The official API takes max_completion_tokens for every chat model; max_tokens is
            # rejected by the reasoning models.
            quirks.add(_MAX_TOKENS)
            if _REASONING_MODEL.match(model.rsplit("/", 1)[-1]):
                quirks.add(_NO_TEMPERATURE)
        return quirks

    def _learn(self, model: str, status: int, text: str) -> bool:
        """Remember the change a rejected request asks for; True when retrying could now succeed."""
        quirk = _quirk_for(status, text)
        if quirk is None or quirk in self._quirks_for(model):
            return False
        self._quirks.setdefault(model, set()).add(quirk)
        return True

    def _body(self, req: CompletionRequest, stream: bool) -> dict[str, Any]:
        quirks = self._quirks_for(req.model)
        tokens_key = "max_completion_tokens" if _MAX_TOKENS in quirks else "max_tokens"
        body: dict[str, Any] = {
            "model": req.model,
            "messages": _to_openai_messages(req.messages),
            tokens_key: req.max_tokens,
            "stream": stream,
        }
        if req.temperature is not None and _NO_TEMPERATURE not in quirks:
            body["temperature"] = req.temperature
        if req.stop:
            body["stop"] = req.stop
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        if req.effort and self.kind == "openai":
            body["reasoning_effort"] = {"xhigh": "high", "max": "high"}.get(req.effort, req.effort)
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in req.tools
            ]
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self.client.post(f"{self.base_url}{path}", json=body, headers=self.headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: timeout", provider=self.name, retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name}: connection error", provider=self.name, retryable=True) from exc
        if resp.status_code >= 400:
            ra = resp.headers.get("retry-after")
            raise ProviderError.from_status(
                self.name, resp.status_code, resp.text, float(ra) if ra and ra.isdigit() else None
            )
        return resp

    def _usage(self, model: str, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        details = raw.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0) or 0
        usage = Usage(
            input_tokens=max((raw.get("prompt_tokens", 0) or 0) - cached, 0),
            output_tokens=raw.get("completion_tokens", 0) or 0,
            cache_read_tokens=cached,
        )
        usage.cost_usd = estimate_cost(model, usage)
        return usage

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        for attempt in range(3):  # at most one retry per known parameter mismatch
            try:
                resp = await self._post("/chat/completions", self._body(request, stream=False))
                break
            except ProviderError as exc:
                if attempt == 2 or not self._learn(request.model, exc.status or 0, str(exc)):
                    raise
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = [
            ToolCall(
                id=c.get("id") or f"call_{i}",
                name=c["function"]["name"],
                arguments=_parse_args(c["function"].get("arguments")),
            )
            for i, c in enumerate(msg.get("tool_calls") or [])
        ]
        model = data.get("model") or request.model
        return CompletionResponse(
            provider=self.name,
            model=model,
            content=msg.get("content") or "",
            thinking=msg.get("reasoning_content") or msg.get("reasoning") or "",
            tool_calls=calls,
            finish_reason=_FINISH.get(choice.get("finish_reason") or "", "stop"),
            usage=self._usage(model, data.get("usage")),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        for attempt in range(3):
            try:
                async for event in self._stream(request):
                    yield event
                return
            except _Rejected as exc:
                # Rejected before any output was sent, so retrying with the adjusted request is safe.
                if attempt == 2 or not self._learn(request.model, exc.error.status or 0, str(exc.error)):
                    raise exc.error from None

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        body = self._body(request, stream=True)
        text: list[str] = []
        thinking: list[str] = []
        partial: dict[int, dict[str, Any]] = {}
        finish = "stop"
        usage_raw: dict[str, Any] | None = None
        model = request.model
        try:
            async with self.client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=self.headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _Rejected(ProviderError.from_status(self.name, resp.status_code, resp.text))
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    model = chunk.get("model") or model
                    usage_raw = chunk.get("usage") or usage_raw
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text.append(delta["content"])
                            yield StreamEvent(type="text", text=delta["content"])
                        r = delta.get("reasoning_content") or delta.get("reasoning")
                        if r:
                            thinking.append(r)
                            yield StreamEvent(type="thinking", text=r)
                        for tc in delta.get("tool_calls") or []:
                            slot = partial.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name}: stream interrupted", provider=self.name, retryable=True
            ) from exc
        calls = [
            ToolCall(id=s["id"] or f"call_{i}", name=s["name"], arguments=_parse_args(s["args"]))
            for i, s in sorted(partial.items())
        ]
        for c in calls:
            yield StreamEvent(type="tool_call", tool_call=c)
        yield StreamEvent(
            type="done",
            response=CompletionResponse(
                provider=self.name,
                model=model,
                content="".join(text),
                thinking="".join(thinking),
                tool_calls=calls,
                finish_reason=_FINISH.get(finish, "stop"),
                usage=self._usage(model, usage_raw),
            ),
        )

    async def list_models(self) -> list[str]:
        try:
            resp = await self.client.get(f"{self.base_url}/models", headers=self.headers)
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name}: connection error", provider=self.name, retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError.from_status(self.name, resp.status_code, resp.text)
        return sorted(m["id"] for m in resp.json().get("data", []) if "id" in m)

    async def aclose(self) -> None:
        await self.client.aclose()
