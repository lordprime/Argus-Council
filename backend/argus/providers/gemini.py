"""Google Gemini provider (Generative Language REST API)."""

from __future__ import annotations

import json
import uuid
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
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "PROHIBITED_CONTENT": "refusal",
}

# JSON-Schema keywords Gemini's OpenAPI-subset schema rejects.
_UNSUPPORTED_SCHEMA_KEYS = {"$schema", "additionalProperties", "$id", "$ref", "definitions", "$defs"}


def _clean_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(schema, list):
        return [_clean_schema(s) for s in schema]
    return schema


def _to_contents(messages: list[Message]) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for c in m.tool_calls:
                names[c.id] = c.name
                parts.append({"functionCall": {"name": c.name, "args": c.arguments}})
            out.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif m.role == "tool":
            part = {
                "functionResponse": {
                    "name": names.get(m.tool_call_id or "", m.name or "tool"),
                    "response": {"error" if m.is_error else "result": m.content},
                }
            }
            if out and out[-1]["role"] == "user" and "functionResponse" in out[-1]["parts"][0]:
                out[-1]["parts"].append(part)
            else:
                out.append({"role": "user", "parts": [part]})
        else:
            out.append({"role": "user", "parts": [{"text": m.content}]})
    return out


class GeminiProvider(Provider):
    kind = "gemini"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        name: str | None = None,
        timeout: float = 600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["x-goog-api-key"] = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    def _body(self, req: CompletionRequest) -> dict[str, Any]:
        cfg: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
        if req.temperature is not None:
            cfg["temperature"] = req.temperature
        if req.stop:
            cfg["stopSequences"] = req.stop
        if req.json_mode:
            cfg["responseMimeType"] = "application/json"
        body: dict[str, Any] = {"contents": _to_contents(req.messages), "generationConfig": cfg}
        if req.system_prompt:
            body["systemInstruction"] = {"parts": [{"text": req.system_prompt}]}
        if req.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _clean_schema(t.input_schema),
                        }
                        for t in req.tools
                    ]
                }
            ]
        return body

    def _parse(self, model: str, data: dict[str, Any]) -> CompletionResponse:
        cand = (data.get("candidates") or [{}])[0]
        text, thinking, calls = [], [], []
        for part in (cand.get("content") or {}).get("parts") or []:
            if "functionCall" in part:
                fc = part["functionCall"]
                calls.append(
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:12]}", name=fc["name"], arguments=fc.get("args") or {}
                    )
                )
            elif part.get("thought"):
                thinking.append(part.get("text", ""))
            elif "text" in part:
                text.append(part["text"])
        um = data.get("usageMetadata") or {}
        cached = um.get("cachedContentTokenCount", 0) or 0
        usage = Usage(
            input_tokens=max((um.get("promptTokenCount", 0) or 0) - cached, 0),
            output_tokens=(um.get("candidatesTokenCount", 0) or 0) + (um.get("thoughtsTokenCount", 0) or 0),
            cache_read_tokens=cached,
        )
        usage.cost_usd = estimate_cost(model, usage)
        finish = "tool_calls" if calls else _FINISH.get(cand.get("finishReason") or "STOP", "stop")
        return CompletionResponse(
            provider=self.name,
            model=data.get("modelVersion") or model,
            content="".join(text),
            thinking="".join(thinking),
            tool_calls=calls,
            finish_reason=finish,
            usage=usage,
        )

    def _path(self, model: str, method: str) -> str:
        model = model.removeprefix("models/")
        return f"{self.base_url}/models/{model}:{method}"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            resp = await self.client.post(
                self._path(request.model, "generateContent"), json=self._body(request), headers=self.headers
            )
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name}: connection error", provider=self.name, retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError.from_status(self.name, resp.status_code, resp.text)
        return self._parse(request.model, resp.json())

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        merged: dict[str, Any] = {"candidates": [{"content": {"parts": []}}]}
        try:
            async with self.client.stream(
                "POST",
                self._path(request.model, "streamGenerateContent") + "?alt=sse",
                json=self._body(request),
                headers=self.headers,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ProviderError.from_status(self.name, resp.status_code, resp.text)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = json.loads(line[5:].strip())
                    cand = (chunk.get("candidates") or [{}])[0]
                    for part in (cand.get("content") or {}).get("parts") or []:
                        merged["candidates"][0]["content"]["parts"].append(part)
                        if "text" in part and not part.get("thought"):
                            yield StreamEvent(type="text", text=part["text"])
                        elif part.get("thought"):
                            yield StreamEvent(type="thinking", text=part.get("text", ""))
                    if cand.get("finishReason"):
                        merged["candidates"][0]["finishReason"] = cand["finishReason"]
                    for k in ("usageMetadata", "modelVersion"):
                        if k in chunk:
                            merged[k] = chunk[k]
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name}: stream interrupted", provider=self.name, retryable=True
            ) from exc
        response = self._parse(request.model, merged)
        for c in response.tool_calls:
            yield StreamEvent(type="tool_call", tool_call=c)
        yield StreamEvent(type="done", response=response)

    async def list_models(self) -> list[str]:
        resp = await self.client.get(f"{self.base_url}/models", headers=self.headers)
        if resp.status_code >= 400:
            raise ProviderError.from_status(self.name, resp.status_code, resp.text)
        return sorted(m["name"].removeprefix("models/") for m in resp.json().get("models", []))

    async def aclose(self) -> None:
        await self.client.aclose()
