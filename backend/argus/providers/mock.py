"""Deterministic offline provider for tests, demos and keyless development."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from argus.providers.base import Provider
from argus.providers.types import CompletionRequest, CompletionResponse, ToolCall, Usage

Responder = Callable[[CompletionRequest], "CompletionResponse | str | dict[str, Any]"]


class MockProvider(Provider):
    """Replies from a script, a responder function, or a deterministic echo.

    A script entry may be a string (text reply) or a dict with ``content`` and
    optional ``tool_calls`` (``[{"name": ..., "arguments": {...}}]``).
    """

    kind = "mock"

    def __init__(
        self, name: str | None = None, *, script: list[Any] | None = None, responder: Responder | None = None
    ) -> None:
        super().__init__(name)
        self.script = list(script or [])
        self.responder = responder
        self.calls: list[CompletionRequest] = []

    def _build(self, req: CompletionRequest, item: Any) -> CompletionResponse:
        if isinstance(item, CompletionResponse):
            return item
        if isinstance(item, str):
            item = {"content": item}
        calls = [
            ToolCall(
                id=c.get("id") or f"call_{i}_{len(self.calls)}",
                name=c["name"],
                arguments=c.get("arguments", {}),
            )
            for i, c in enumerate(item.get("tool_calls") or [])
        ]
        content = item.get("content", "")
        prompt_chars = sum(len(m.content) for m in req.messages)
        return CompletionResponse(
            provider=self.name,
            model=req.model,
            content=content,
            thinking=item.get("thinking", ""),
            tool_calls=calls,
            finish_reason=item.get("finish_reason") or ("tool_calls" if calls else "stop"),
            usage=Usage(input_tokens=max(prompt_chars // 4, 1), output_tokens=max(len(content) // 4, 1)),
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        if self.script:
            return self._build(request, self.script.pop(0))
        if self.responder:
            return self._build(request, self.responder(request))
        if request.metadata.get("phase"):
            return self._build(request, council_responder(request))
        last = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        digest = hashlib.sha256(last.encode()).hexdigest()[:8]
        return self._build(request, f"[{request.model}#{digest}] {last[:400]}")

    async def list_models(self) -> list[str]:
        return ["mock-1", "mock-large"]


def council_responder(req: CompletionRequest) -> dict[str, Any]:
    """Plausible, deterministic council behaviour for offline demos and tests."""
    import json

    meta = req.metadata
    phase = meta.get("phase")
    topic = meta.get("topic") or "the topic"
    role = meta.get("role") or "agent"
    if phase == "plan":
        mode = meta.get("mode", "general")
        plans = {
            "research": [
                ("Scope the questions", "analyst"),
                ("Gather evidence", "researcher"),
                ("Analyse findings", "analyst"),
                ("Draft the report", "writer"),
            ],
            "coding": [("Design the solution", "architect"), ("Implement", "coder"), ("Test", "tester")],
            "chat": [("Answer", "assistant")],
        }.get(mode, [("Analyse the request", "analyst"), ("Produce the work", "writer")])
        steps = [
            {
                "id": f"s{i + 1}",
                "title": t,
                "assignee": r,
                "instructions": f"{t} for: {topic}",
                "depends_on": [f"s{i}"] if i else [],
            }
            for i, (t, r) in enumerate(plans)
        ]
        return {"content": json.dumps({"steps": steps})}
    if phase == "review":
        return {
            "content": json.dumps(
                {
                    "action": "pass",
                    "reason": "scope",
                    "severity": "low",
                    "note": "On track.",
                    "correction": None,
                }
            )
        }
    if phase == "vote":
        return {"content": json.dumps({"option": "approve", "rationale": "Meets the brief."})}
    if phase == "synthesize":
        return {
            "content": f"# {topic}\n\n## Summary\nOffline demo deliverable produced by the mock council.\n\n"
            "## Findings\n- Planned, executed, reviewed and approved by the council.\n"
        }
    return {"content": f"{role.title()} notes on {topic}: completed the assigned work (offline demo)."}
