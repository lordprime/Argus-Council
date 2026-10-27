"""Thought bus: every agent thought is reviewed before it is acted upon.

Reviewers run in priority order ``user > guardrails > loop detector > LLM
overseers (security > monitor > critic)``. Each returns a verdict:

* ``pass``     - nothing to do
* ``annotate`` - comment; the agent continues and sees the note next turn
* ``hold``     - the thought is not acted on; the agent regenerates with the correction
* ``block``    - the thought is discarded; the agent is told why and must change course

``oversight.intercept_mode`` caps how far LLM overseers may go (deterministic
guardrail blocks are never downgraded).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

from argus.core.logging import get_logger
from argus.core.metrics import INTERCEPTS, OVERSIGHT_FAILURES, intercept_reason_label, role_label
from argus.providers.types import CompletionRequest, Message, ToolCall
from argus.runtime.events import Actor
from argus.runtime.guardrails import Violation

log = get_logger(__name__)

VerdictAction = Literal["pass", "annotate", "hold", "block"]
_RANK: dict[str, int] = {"pass": 0, "annotate": 1, "hold": 2, "block": 3}
_ROLE_PRIORITY = {"security": 0, "monitor": 1, "critic": 2}


@dataclass
class Thought:
    thought_id: str
    member: Actor
    text: str
    tool_calls: list[ToolCall]
    step_id: str | None
    step_title: str
    final: bool  # True when the agent proposes this as its step result (no tool calls)
    event_id: str | None = None


@dataclass
class Verdict:
    action: VerdictAction
    reason: str = "scope"
    severity: Literal["low", "medium", "high"] = "medium"
    note: str = ""
    correction: str | None = None
    by: Actor = field(default_factory=Actor.system)
    deterministic: bool = False


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model output (handles code fences and prose)."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for c in candidates:
        try:
            val = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict):
            return val
    return None


class LoopDetector:
    """Flags agents that repeat the same action or near-identical thoughts."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.history: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=threshold * 2))

    @staticmethod
    def signature(thought: Thought) -> str:
        if thought.tool_calls:
            body = json.dumps(
                [[c.name, c.arguments] for c in thought.tool_calls], sort_keys=True, default=str
            )
        else:
            body = re.sub(r"\W+", " ", thought.text.lower()).strip()[:2000]
        return hashlib.sha1(body.encode(), usedforsecurity=False).hexdigest()

    def check(self, thought: Thought) -> Verdict | None:
        key = thought.member.member_id or ""
        sig = self.signature(thought)
        hist = self.history[key]
        hist.append(sig)
        repeats = sum(1 for s in hist if s == sig)
        if repeats >= self.threshold:
            return Verdict(
                action="block",
                reason="loop",
                severity="high" if repeats > self.threshold else "medium",
                note=f"Same action repeated {repeats} times",
                correction=(
                    "You are repeating the same action without progress. Stop, summarise what you have, "
                    "and either try a materially different approach or finish the step with what you know."
                ),
                by=Actor(kind="system", name="Loop detector", overseer=True),
                deterministic=True,
            )
        return None


class OverseerAgent:
    """An overseer council member (monitor / critic / security) reviewing thoughts via its LLM."""

    REVIEW_FORMAT = (
        'Respond with ONLY a JSON object: {"action": "pass"|"annotate"|"hold"|"block", '
        '"reason": "drift"|"loop"|"scope"|"guardrail"|"factual"|"safety", '
        '"severity": "low"|"medium"|"high", "note": "<short why>", '
        '"correction": "<instruction to the agent or null>"}. '
        "Use pass unless there is a concrete problem. hold = must be redone with your correction; "
        "block = unsafe or clearly wrong, must not be acted on."
    )

    def __init__(self, actor: Actor, provider: str, model: str, system_prompt: str, fallbacks=None) -> None:
        self.actor = actor
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.fallbacks = fallbacks or []

    @property
    def role(self) -> str:
        return self.actor.role or "monitor"

    def wants(self, thought: Thought, sensitivity: str) -> bool:
        if sensitivity == "off" or thought.member.member_id == self.actor.member_id:
            return False
        has_tools = bool(thought.tool_calls)
        has_code = "```" in thought.text
        if self.role == "security":
            return has_tools or has_code
        if self.role == "critic":
            return thought.final and sensitivity in ("medium", "high")
        # monitor
        if sensitivity == "high":
            return True
        if sensitivity == "medium":
            return thought.final or has_tools
        return has_tools

    def _prompt(self, thought: Thought, ctx: Any) -> list[Message]:
        calls = (
            "\n".join(
                f"- {c.name}({json.dumps(c.arguments, default=str)[:1500]})" for c in thought.tool_calls
            )
            or "(none)"
        )
        body = (
            f"Council topic: {ctx.topic}\nUser description: {ctx.description[:2000]}\n"
            f"Step under way: {thought.step_title}\n\n"
            f"Agent: {thought.member.name} ({thought.member.role})\n"
            f"Thought{' (proposed final result)' if thought.final else ''}:\n"
            f"<<<\n{thought.text[:12000]}\n>>>\n"
            f"Proposed tool calls:\n{calls}\n\n{self.REVIEW_FORMAT}"
        )
        return [Message.system(self.system_prompt), Message.user(body)]

    async def review(self, thought: Thought, ctx: Any) -> Verdict | None:
        from argus.services.llm import Route

        req = CompletionRequest(
            model=self.model,
            messages=self._prompt(thought, ctx),
            max_tokens=800,
            effort="low",
            metadata={"phase": "review", "role": self.role, "cache": False},
        )
        try:
            resp = await ctx.llm.complete(
                req,
                Route(self.provider, self.model),
                db=None,
                owner_id=ctx.owner_id,
                fallbacks=self.fallbacks,
                ledger=ctx.ledger,
            )
        except Exception as exc:
            OVERSIGHT_FAILURES.labels(role_label(self.actor.role)).inc()
            log.warning("oversight.review_failed", overseer=self.actor.name, error=str(exc)[:200])
            return None
        ctx.engine.record_usage(resp.usage)
        ctx.by_member[self.actor.member_id or ""]["usd"] += resp.usage.cost_usd
        ctx.by_member[self.actor.member_id or ""]["tokens"] += resp.usage.total_tokens
        data = parse_json_object(resp.content) or {}
        action = str(data.get("action", "pass")).lower()
        if action not in _RANK:
            action = "pass"
        severity = data.get("severity") if data.get("severity") in ("low", "medium", "high") else "medium"
        correction = data.get("correction")
        return Verdict(
            action=action,  # type: ignore[arg-type]
            reason=str(data.get("reason") or "scope")[:40],
            severity=severity,  # type: ignore[arg-type]
            note=str(data.get("note") or "")[:1000],
            correction=str(correction)[:4000] if correction else None,
            by=self.actor,
        )


class ThoughtBus:
    def __init__(self, ctx: Any, overseers: list[OverseerAgent]) -> None:
        self.ctx = ctx
        self.overseers = sorted(overseers, key=lambda o: _ROLE_PRIORITY.get(o.role, 9))
        rails = ctx.engine.rails
        self.loops = LoopDetector(rails.oversight.loop_threshold)

    def _guardrail_verdicts(self, thought: Thought) -> list[Verdict]:
        out: list[Verdict] = []
        engine = self.ctx.engine
        guard = Actor(kind="system", name="Guardrails", overseer=True)
        for v in engine.screen_text(thought.text):
            if v.action == "blocked":
                out.append(
                    Verdict(
                        "block",
                        "guardrail",
                        "high",
                        v.message,
                        f"Your output violates a guardrail ({v.rule}): {v.message}. Remove it.",
                        guard,
                        deterministic=True,
                    )
                )
        for call in thought.tool_calls:
            v2: Violation | None = engine.check_tool_args(call.name, call.arguments)
            if v2 is not None:
                out.append(
                    Verdict(
                        "block",
                        "guardrail",
                        "high",
                        v2.message,
                        f"The call {call.name} was blocked: {v2.message}. Use a safe alternative.",
                        guard,
                        deterministic=True,
                    )
                )
        return out

    async def review(self, thought: Thought) -> Verdict:
        """Run all reviewers, emit intercepts/corrections, and return the effective verdict."""
        rails = self.ctx.engine.rails
        verdicts = self._guardrail_verdicts(thought)
        loop = self.loops.check(thought)
        if loop:
            verdicts.append(loop)
        if not any(v.action == "block" and v.deterministic for v in verdicts):
            wanted = [o for o in self.overseers if o.wants(thought, rails.oversight.monitor_sensitivity)]
            results = await asyncio.gather(*(o.review(thought, self.ctx) for o in wanted))
            ceiling = _RANK[rails.oversight.intercept_mode]
            for v in results:
                if v is None or v.action == "pass":
                    continue
                if _RANK[v.action] > ceiling:
                    v.action = rails.oversight.intercept_mode
                verdicts.append(v)

        effective = Verdict("pass")
        for v in verdicts:
            iid = f"int_{uuid.uuid4().hex[:12]}"
            INTERCEPTS.labels("overseer", intercept_reason_label(v.reason), v.action).inc()
            env = await self.ctx.sink.emit(
                "intercept.raised",
                {
                    "intercept_id": iid,
                    "target": {"thought_id": thought.thought_id, "member_id": thought.member.member_id},
                    "by": "overseer",
                    "reason": v.reason,
                    "severity": v.severity,
                    "action": v.action,
                    "note": v.note,
                },
                actor=v.by,
                step_id=thought.step_id,
                causation_id=thought.event_id,
            )
            if v.correction:
                await self.ctx.sink.emit(
                    "correction.issued",
                    {
                        "correction_id": f"cor_{uuid.uuid4().hex[:12]}",
                        "intercept_id": iid,
                        "target_member_id": thought.member.member_id,
                        "text": v.correction,
                        "scope": "thought",
                    },
                    actor=v.by,
                    step_id=thought.step_id,
                    causation_id=env.id,
                )
            if _RANK[v.action] > _RANK[effective.action]:
                effective = v
        if len(verdicts) > 1 and effective.action != "pass":
            # Merge every correction so the agent addresses all of them at once.
            notes = [f"[{v.by.name}] {v.correction}" for v in verdicts if v.correction]
            effective = Verdict(
                effective.action,
                effective.reason,
                effective.severity,
                effective.note,
                "\n".join(notes) or effective.correction,
                effective.by,
                effective.deterministic,
            )
        return effective
