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
    if isinstance(phase, str) and phase.startswith("report_"):
        return {"content": json.dumps(_report_reply(phase[7:], topic, req))}
    if phase == "synthesize":
        return {
            "content": f"# {topic}\n\n## Summary\nOffline demo deliverable produced by the mock council.\n\n"
            "## Findings\n- Planned, executed, reviewed and approved by the council.\n"
        }
    return {
        "content": f"{role.title()} notes on {topic}: completed the assigned work (offline demo). "
        "Demo figures: Option A scored 72 points, Option B 64 and Option C 58. Adoption rose from 12% "
        "in 2024 to 18% in 2025, 27% in 2026 and 35% in 2027, against a baseline of 10, 12, 14 and 15. "
        "Option A leads by 8 points over Option B."
    }


def _report_reply(stage: str, topic: str, req: CompletionRequest) -> dict[str, Any]:
    """Deterministic answers for the report pipeline's stages (see ``argus.reports.pipeline``)."""
    import re

    task = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    sections = [
        ("context", "Why the question matters", "Background and the decision at stake."),
        ("evidence", "What the evidence shows", "The main findings, quantified."),
        ("options", "How the options compare", "A side-by-side comparison."),
        ("outlook", "Where it is heading", "Trends and timelines."),
        ("implications", "What it means for the reader", "Consequences and trade-offs."),
        ("gaps", "What we still do not know", "Open questions and uncertainty."),
        ("adoption", "What adoption would take", "Costs, skills and sequencing."),
    ]
    if stage == "outline":
        return {
            "title": topic,
            "subtitle": "An offline demo report from the mock council",
            "audience": "Decision makers",
            "sections": [
                {
                    "id": sid,
                    "heading": heading,
                    "purpose": purpose,
                    "key_points": [f"{heading}: point one", f"{heading}: point two"],
                    "visuals": [{"kind": "bar", "show": "the main quantity"}]
                    if sid in ("evidence", "outlook")
                    else [],
                }
                for sid, heading, purpose in sections
            ],
        }
    if stage == "review":
        return {"verdict": "approve", "notes": ""}
    if stage == "data":
        return {
            "charts": [
                {
                    "id": "d1",
                    "section_id": "evidence",
                    "kind": "bar",
                    "title": "Scores by option",
                    "categories": ["Option A", "Option B", "Option C"],
                    "series": [{"name": "Score", "values": [72, 64, 58]}],
                    "unit": "points",
                    "source": "Council assessment (demo data)",
                },
                {
                    "id": "d2",
                    "section_id": "outlook",
                    "kind": "line",
                    "title": "Adoption over time",
                    "categories": ["2024", "2025", "2026", "2027"],
                    "series": [
                        {"name": "Adoption", "values": [12, 18, 27, 35]},
                        {"name": "Baseline", "values": [10, 12, 14, 15]},
                    ],
                    "unit": "%",
                    "source": "Council assessment (demo data)",
                },
            ],
            "tables": [
                {
                    "id": "t1",
                    "section_id": "options",
                    "title": "Options compared",
                    "columns": ["Option", "Strength", "Weakness", "Score"],
                    "rows": [
                        ["Option A", "Mature", "Costly", "72"],
                        ["Option B", "Cheap", "Young", "64"],
                        ["Option C", "Simple", "Limited", "58"],
                    ],
                    "source": "Council assessment (demo data)",
                }
            ],
            "metrics": [
                {
                    "label": "Leading option score",
                    "value": "72",
                    "unit": "points",
                    "trend": "up",
                    "delta": "+8 vs B",
                },
                {"label": "Adoption by 2027", "value": "35", "unit": "%", "trend": "up"},
            ],
        }
    if stage in ("section", "revise"):
        m = re.search(r"Section id: ([\w-]+)", task)
        sid = m.group(1) if m else "section"
        refs = {"evidence": "d1", "outlook": "d2", "options": "t1"}
        text = (
            f"This section of the offline demo examines {topic} from the angle of '{sid}'. The mock council "
            "weighed the evidence it gathered, compared the options against cost, maturity and risk, and "
            "recorded how confident it is in each conclusion. In a real run this text is written by a "
            "section author from the sources the researchers collected, with every claim tied to a citation."
        )
        blocks: list[dict[str, Any]] = [
            {"type": "paragraph", "text": text},
            {"type": "paragraph", "text": text.replace("examines", "also considers")},
            {"type": "paragraph", "text": text.replace("examines", "draws conclusions on")},
            {
                "type": "bullets",
                "items": [
                    "The leading option scores highest overall.",
                    "Costs fall as adoption grows.",
                    "Evidence quality varies by source.",
                ],
            },
        ]
        if sid in refs:
            blocks.append({"type": "chart" if refs[sid].startswith("d") else "table", "ref": refs[sid]})
        if sid == "gaps":
            blocks.append(
                {"type": "callout", "tone": "warning", "title": "Caveat", "text": "Demo data, not evidence."}
            )
        return {"summary": f"The key takeaway on {sid}.", "blocks": blocks}
    if stage == "frame":
        para = (
            f"This offline demo report on {topic} was planned, drafted, reviewed and approved by the mock "
            "council. It shows the shape of a real report: a summary a decision maker can act on, findings "
            "with confidence levels, recommendations by priority, a risk register and the quality checks the "
            "report passed before delivery."
        )
        return {
            "executive_summary": para + "\n\n" + para.replace("This offline demo report", "The report"),
            "key_findings": [
                {
                    "statement": "Option A leads on overall score.",
                    "confidence": "high",
                    "evidence": "Scores by option",
                },
                {
                    "statement": "Adoption is rising year on year.",
                    "confidence": "medium",
                    "evidence": "Adoption over time",
                },
                {
                    "statement": "Long-term costs remain uncertain.",
                    "confidence": "low",
                    "evidence": "Few sources",
                },
            ],
            "recommendations": [
                {
                    "action": "Pilot Option A",
                    "rationale": "Highest score",
                    "priority": "high",
                    "owner": "Team",
                    "horizon": "Q1",
                },
                {"action": "Track adoption quarterly", "rationale": "Trend is moving", "priority": "medium"},
                {"action": "Revisit costs in a year", "rationale": "Uncertain", "priority": "low"},
            ],
            "risks": [
                {"risk": "Costs overrun", "likelihood": 3, "impact": 4, "mitigation": "Stage the rollout"},
                {"risk": "Vendor lock-in", "likelihood": 2, "impact": 3, "mitigation": "Keep an exit plan"},
            ],
            "methodology": "Offline demo: every agent answered from the mock provider's scripted behaviour.",
            "limitations": ["Demo data only", "No live sources"],
            "glossary": [{"term": "Council", "definition": "The team of agents that produced this report."}],
        }
    if stage == "qa":
        return {"score": 90, "issues": []}
    return {}
