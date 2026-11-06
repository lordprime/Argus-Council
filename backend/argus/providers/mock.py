"""Deterministic offline provider for tests, demos and keyless development."""

from __future__ import annotations

import hashlib
import re
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
    if isinstance(phase, str) and phase.startswith("a2a_"):
        replies = _user_reply_calls(req, role, {t.name for t in req.tools})
        if replies:
            return {"content": "Answering the user first.", "tool_calls": _with_ids(replies, req)}
        return _a2a_answer(phase[4:], topic, role, req)
    if phase == "synthesize":
        return {
            "content": f"# {topic}\n\n## Summary\nOffline demo deliverable produced by the mock council.\n\n"
            "## Findings\n- Planned, executed, reviewed and approved by the council.\n"
        }
    if phase in _DEBATE_PHASES:  # the debate (argus.runtime.debate)
        return _debate_reply(str(phase), topic, req)
    if phase == "spawn":  # a helper's task (argus.tools.spawn)
        return _helper_answer(topic, role, req)
    if phase == "execute":
        calls = _spawn_calls(req, topic, role) + _collaboration_calls(req, topic, role)
        if calls:
            return {
                "content": f"{role.title()} is checking in with the council before finishing.",
                "tool_calls": _with_ids(calls, req),
            }
    return {
        "content": f"{role.title()} notes on {topic}: completed the assigned work (offline demo). "
        "Demo figures: Option A scored 72 points, Option B 64 and Option C 58. Adoption rose from 12% "
        "in 2024 to 18% in 2025, 27% in 2026 and 35% in 2027, against a baseline of 10, 12, 14 and 15. "
        "Option A leads by 8 points over Option B."
    }


# ---------------------------------------------------------------- council communication (A2A)
# The mock council talks the way a real one does, cheaply and deterministically: on the first turn of
# a step some roles ask a teammate, post to the board or ask for a review (see argus.tools.council),
# and every member answers a message the user sent during the run with reply_to_user.

_ROSTER_LINE = re.compile(r"^- (.+) \(([^()]+)\)( \(you\))?$")
_USER_MESSAGE = re.compile(r"^\[A2A message(?: (\S+))? from the user\]")
# Runtime helpers: a researcher splits a comparison ("A vs B", "compare A and B") into one helper per
# side, spawned at once on the first turn of its step (argus.tools.spawn).
_COMPARISON = re.compile(r"\b(?:vs\.?|versus|compare[ds]?|comparison)\b", re.I)
_SIDES = re.compile(r"\s+(?:vs\.?|versus|and|or|with)\s+", re.I)


def _spawn_calls(req: CompletionRequest, topic: str, role: str) -> list[dict[str, Any]]:
    """Two helpers in parallel, one per side of a comparison, on the researcher's first turn."""
    if role != "researcher" or "spawn_agents" not in {t.name for t in req.tools}:
        return []
    if not _COMPARISON.search(topic) or any(m.role == "tool" for m in req.messages):
        return []
    subject = re.sub(r"(?i)^\s*(?:a\s+)?(?:compare|comparison\s+of|comparing)\s+", "", topic).strip()
    sides = [s.strip(" .?") for s in _SIDES.split(subject, maxsplit=1) if s.strip(" .?")]
    if len(sides) < 2:
        sides = [f"{subject} (the case for)", f"{subject} (the case against)"]
    return [
        {
            "name": "spawn_agents",
            "arguments": {
                "agents": [
                    {"role": "researcher", "instructions": f"Gather evidence on {side} for: {topic}"}
                    for side in sides[:2]
                ],
                "wait": True,
            },
        }
    ]


def _helper_answer(topic: str, role: str, req: CompletionRequest) -> dict[str, Any]:
    task = next((m.content for m in req.messages if m.role == "user"), "")
    focus = next((line for line in task.splitlines() if line.startswith("Gather evidence on ")), "")
    on = focus[len("Gather evidence on ") :].split(" for: ", 1)[0] if focus else topic
    return {
        "content": f"Helper findings on {on} ({role}, offline demo): two sources agree; Option A scored 72 "
        "points against 64 for Option B, and adoption rose from 12% in 2024 to 35% in 2027."
    }


def _with_ids(calls: list[dict[str, Any]], req: CompletionRequest) -> list[dict[str, Any]]:
    """Give each call a deterministic id, unique within the conversation, so the dicts are valid
    ``ToolCall`` data for callers that build a ``CompletionResponse`` from them directly."""
    turn = len(req.messages)
    return [{"id": f"mock_{turn}_{i}_{c['name']}", **c} for i, c in enumerate(calls)]


def _roster(req: CompletionRequest) -> dict[str, str]:
    """Teammates (role -> name, excluding the caller) from the prompt's "Your council" section."""
    out: dict[str, str] = {}
    lines = req.system_prompt.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "## Your council (A2A agents)")
    except StopIteration:
        return out
    for line in lines[start + 1 :]:
        m = _ROSTER_LINE.match(line.strip())
        if not m:
            break
        if not m.group(3):
            out.setdefault(m.group(2).strip().lower(), m.group(1).strip())
    return out


def _task_title(req: CompletionRequest) -> str:
    first = next((m.content for m in req.messages if m.role == "user"), "")
    task = first.split("Your task:", 1)[-1].strip()
    return (task.splitlines() or ["the task"])[0][:160]


def _user_reply_calls(req: CompletionRequest, role: str, tools: set[str]) -> list[dict[str, Any]]:
    """reply_to_user once for each user message this conversation received and has not answered."""
    if "reply_to_user" not in tools:
        return []
    answered = {
        c.arguments.get("message_id")
        for m in req.messages
        if m.role == "assistant"
        for c in m.tool_calls
        if c.name == "reply_to_user"
    }
    calls: list[dict[str, Any]] = []
    for m in req.messages:
        hit = _USER_MESSAGE.match(m.content) if m.role == "user" else None
        if hit is None or hit.group(1) in answered:
            continue
        answered.add(hit.group(1))
        said = m.content[hit.end() :].strip()[:200]
        calls.append(
            {
                "name": "reply_to_user",
                "arguments": {
                    "text": f'Noted, I am taking this into account in my work as the {role}: "{said}".',
                    **({"message_id": hit.group(1)} if hit.group(1) else {}),
                },
            }
        )
    return calls


def _collaboration_calls(req: CompletionRequest, topic: str, role: str) -> list[dict[str, Any]]:
    """The A2A exchanges a role makes on the first turn of a step (at most one exchange per seat)."""
    tools = {t.name for t in req.tools}
    calls = _user_reply_calls(req, role, tools)
    if any(m.role == "tool" for m in req.messages):
        return calls  # not the first turn: the exchange already happened
    me = str(req.metadata.get("member") or role)
    board = (
        req.system_prompt.split("## Council board", 1)[-1] if "## Council board" in req.system_prompt else ""
    )
    team = _roster(req)
    title = _task_title(req)
    if role in ("researcher", "assistant"):
        peer = "analyst" if role == "researcher" else "researcher"
        if "ask_agent" in tools and peer in team and f"Evidence brief from {me}" not in board:
            calls.append(
                {
                    "name": "ask_agent",
                    "arguments": {
                        "to": peer,
                        "question": f"For '{title}' on {topic}: which questions and figures matter most, "
                        "and what should I look for to confirm them?",
                    },
                }
            )
            if "notes_write" in tools:
                calls.append(
                    {
                        "name": "notes_write",
                        "arguments": {
                            "title": f"Evidence brief from {me}",
                            "body": f"Working notes on {topic}: Option A scored 72 points, Option B 64 and "
                            "Option C 58; adoption rose from 12% in 2024 to 35% in 2027 (demo data).",
                            "tags": ["evidence"],
                        },
                    }
                )
    elif role in ("analyst", "coder"):
        reviewer = next(
            (r for r in (("critic", "writer") if role == "analyst" else ("critic", "tester")) if r in team),
            None,
        )
        if "request_review" in tools and reviewer and f"Review of {me}'s work" not in board:
            calls.append(
                {
                    "name": "request_review",
                    "arguments": {
                        "to": reviewer,
                        "work": f"Draft for '{title}' on {topic}: Option A leads with 72 points, 8 ahead of "
                        "Option B (64) and 14 ahead of Option C (58). Adoption is rising year on year.",
                        "criteria": "correctness, evidence, clarity",
                    },
                }
            )
    claim = _CLAIMS.get(role)
    if claim and "make_claim" in tools and claim[0] not in req.system_prompt:
        statement, source, quote = claim
        calls.append(
            {
                "name": "make_claim",
                "arguments": {
                    "statement": statement,
                    "evidence": [{"source": source, "quote": quote}],
                    "confidence": 0.8,
                },
            }
        )
    return calls


# ---------------------------------------------------------------- the debate (argus.runtime.debate)
# The mock council debates deterministically: researchers and analysts put a claim on record while
# they work; the secret devil's advocate (it recognises its own orders) challenges a teammate's claim
# demanding proof and objects once before it is persuaded; the challenged member defends with
# evidence; the challenger accepts; everyone endorses, the scribe compiles and every author signs.
_DEBATE_PHASES = frozenset({"deliberate_challenge", "deliberate_answer", "position", "compile", "cosign"})
_CLAIMS = {
    "researcher": (
        "Adoption rose from 12% in 2024 to 35% in 2027.",
        "https://example.org/demo/adoption-survey",
        "35% of organisations had adopted it by 2027",
    ),
    "analyst": (
        "Option A leads with 72 points, 8 ahead of Option B.",
        "https://example.org/demo/scores",
        "Option A 72, Option B 64, Option C 58",
    ),
    "architect": (
        "A modular design keeps the components independently testable.",
        "SUMMARY.md",
        "each module has its own interface",
    ),
}
_ADVOCATE_MARK = "strongest case against the emerging view"
_CLAIM_LINE = re.compile(r"^- (clm_\w+) \[(\w+)\] by (.+?): (.*)$")
_CHALLENGE_ID = re.compile(r"^- (chl_\w+) \[")
_WORK_LINE = re.compile(r"^- .+ \((.+?), \w+\): ")


def _section(text: str, heading: str) -> list[str]:
    """The lines of a ``## heading`` section of a prompt."""
    if f"## {heading}" not in text:
        return []
    body = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    return [line for line in body.splitlines()[1:] if line.strip()]


def _matches(text: str, heading: str, pattern: re.Pattern[str], group: int = 1) -> list[str]:
    return [m.group(group) for line in _section(text, heading) if (m := pattern.match(line))]


def _debate_reply(phase: str, topic: str, req: CompletionRequest) -> dict[str, Any]:
    import json

    me = str(req.metadata.get("member") or "")
    users = [m.content for m in req.messages if m.role == "user"]
    # The task itself (teammates' messages read at this turn come after it).
    task = next((u for u in users if "Your task:" in u), users[-1] if users else "")
    advocate = _ADVOCATE_MARK in req.system_prompt
    claims = [m.groups() for line in _section(task, "Claims on record") if (m := _CLAIM_LINE.match(line))]
    if phase == "deliberate_challenge":
        settle = _matches(task, "Answers to your challenges", _CHALLENGE_ID)
        out: dict[str, Any] = {
            "challenges": [],
            "resolutions": [
                {"challenge_id": c, "outcome": "accepted", "note": "The evidence answers my objection."}
                for c in settle
            ],
        }
        if advocate:
            theirs = [c for c in claims if c[2] != me]
            if theirs:
                cid, _, _, statement = theirs[0]
                target, about = cid, statement.split(" (evidence:", 1)[0][:120]
            else:
                names = _matches(task, "The council's work", _WORK_LINE)
                target, about = next((n for n in names if n != me), ""), "the conclusions in this work"
            if target:
                out["challenges"].append(
                    {
                        "target": target,
                        "objection": f"Where is the proof for '{about}'? It may rest on a single favourable "
                        "source; show the primary data.",
                        "demand": "proof",
                    }
                )
        return {"content": json.dumps(out)}
    if phase == "deliberate_answer":
        mine = _matches(task, "Challenges against you", _CHALLENGE_ID)
        return {
            "content": json.dumps(
                {
                    "responses": [
                        {
                            "challenge_id": c,
                            "stance": "defend",
                            "argument": "Two independent demo datasets give the same figures; the primary "
                            "table is cited below.",
                            "evidence": [
                                {
                                    "source": "https://example.org/demo/primary-table",
                                    "quote": "Option A 72, Option B 64, Option C 58; adoption 12% (2024) "
                                    "to 35% (2027)",
                                }
                            ],
                        }
                        for c in mine
                    ]
                }
            )
        }
    if phase == "position":
        first_round = "Converge, round 1:" in task
        theirs = next((c[0] for c in claims if c[2] != me), None)
        stances = []
        for cid, _, _, _ in claims:
            doubt = advocate and first_round and cid == theirs
            stances.append(
                {
                    "claim_id": cid,
                    "stance": "object" if doubt else "endorse",
                    "reason": "The cost side is not yet shown."
                    if doubt
                    else "The evidence and the debate support it.",
                }
            )
        out = {
            "conclusion": f"On {topic}, Option A is the strongest choice on the evidence, and adoption is "
            "rising year on year (offline demo).",
            "stances": stances,
        }
        if advocate and first_round:
            out["alternative"] = "Option B could still win on total cost once licences are counted."
        return {"content": json.dumps(out)}
    if phase == "compile":
        agreed = [s.split(" (", 1)[0] for s in _matches(task, "Agreed claims", _CLAIM_LINE, 4)]
        contested = [
            s.split(" (", 1)[0]
            for s in _matches(task, "Contested claims (give both sides and their evidence)", _CLAIM_LINE, 4)
        ]
        body = "\n".join(f"- {s}" for s in agreed) or "- The council reached no agreed claim."
        text = (
            f"# {topic}\n\n## Summary\nThe council's agreed conclusion (offline demo): Option A is the "
            f"strongest choice, and adoption is rising.\n\n## What the council agreed\n{body}\n"
        )
        if contested:
            text += "\n## Still contested\n" + "\n".join(f"- {s}" for s in contested) + "\n"
        return {"content": text}
    # cosign
    if "## Amendments" in task:
        ids = re.findall(r"^- (am_\d+) by", task, re.M)
        return {"content": json.dumps({"endorse": ids})}
    return {"content": json.dumps({"action": "sign", "reason": "It states what the council agreed."})}


def _a2a_answer(form: str, topic: str, role: str, req: CompletionRequest) -> dict[str, Any]:
    """A teammate's answer to an ask (``request``), a delegated task or a review request."""
    task = next((m.content for m in req.messages if m.role == "user"), "")
    if form == "review":
        work = task.split("--- work under review ---", 1)[-1].split("--- end ---", 1)[0].strip()
        if len(work) < 40:
            return {
                "content": "VERDICT: revise\n- The work is too thin to judge: add the figures and their "
                "sources.\n- State the conclusion in one sentence."
            }
        return {
            "content": f"VERDICT: approve\n- The comparison is clear and the figures are consistent.\n"
            f"- Minor: cite where each figure on {topic} comes from (demo review by the {role})."
        }
    if form == "delegate":
        return {
            "content": f"Done (offline demo). The {role} completed the delegated work on {topic}: Option A "
            "scored 72 points, Option B 64 and Option C 58."
        }
    return {
        "content": f"From the {role}: on {topic}, focus on how the options compare on cost, maturity and "
        "risk. The figures that matter are the scores (A 72, B 64, C 58) and adoption (12% in 2024 to "
        "35% in 2027); confirm them against primary sources. Demo answer from the mock council."
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
