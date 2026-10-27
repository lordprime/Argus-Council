"""Council orchestration: plan → execute (parallel, dependency-aware) → synthesise → vote → deliver."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from argus.core.errors import NotFound
from argus.core.logging import get_logger
from argus.providers.base import ProviderError
from argus.runtime.agent import AgentOutcome, AgentRunner, MemberSpec
from argus.runtime.context import RunContext
from argus.runtime.oversight import OverseerAgent, ThoughtBus, parse_json_object
from argus.tools.base import ToolRegistry

log = get_logger(__name__)

DELIVERABLE_NAMES = {
    "research": "report.md",
    "coding": "SUMMARY.md",
    "chat": "answer.md",
    "general": "deliverable.md",
}

_DEFAULT_PLANS: dict[str, list[tuple[str, str, str]]] = {
    # (title, role, instructions)
    "research": [
        (
            "Scope the question",
            "analyst",
            "Break the topic into 3-6 precise research questions and success criteria.",
        ),
        (
            "Gather evidence",
            "researcher",
            "Collect evidence for each research question; record notes with sources.",
        ),
        (
            "Analyse findings",
            "analyst",
            "Compare evidence, resolve conflicts, quantify uncertainty, list gaps.",
        ),
        ("Draft the report", "writer", "Write the report in the requested format with citations."),
    ],
    "coding": [
        ("Design", "architect", "Design modules, interfaces and file layout for the requested software."),
        ("Implement", "coder", "Implement the design in the workspace with small, verifiable changes."),
        ("Test", "tester", "Write and run tests; report failures with reproduction steps."),
        ("Review & document", "coder", "Fix issues found and write a README describing usage."),
    ],
    "chat": [("Answer", "assistant", "Answer the user's message directly and helpfully.")],
    "general": [
        (
            "Analyse the request",
            "analyst",
            "Clarify the goal, constraints and what a great result looks like.",
        ),
        ("Produce the work", "writer", "Produce the requested output."),
    ],
}


@dataclass
class Step:
    id: str
    title: str
    instructions: str
    assignee: MemberSpec
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    output: str = ""


class CouncilOrchestrator:
    def __init__(self, ctx: RunContext, members: list[MemberSpec], tools: ToolRegistry) -> None:
        self.ctx = ctx
        self.members = members
        self.tools = tools
        self.workers = [m for m in members if not m.overseer] or members
        self.overseers = [m for m in members if m.overseer]
        self.chair = next((m for m in members if m.role == "chair"), self.workers[0])
        self.writer = next((m for m in self.workers if m.role == "writer"), self.chair)
        self.bus = ThoughtBus(
            ctx,
            [
                OverseerAgent(m.actor, m.provider, m.model, m.system_prompt, m.fallbacks)
                for m in self.overseers
            ],
        )
        roster = ", ".join(f"{m.name} ({m.role})" for m in members)
        self.brief = f"Your council: {roster}. Mode: {ctx.mode}."
        self.steps: list[Step] = []
        self.plan_version = 0

    def runner(self, member: MemberSpec) -> AgentRunner:
        return AgentRunner(member, self.ctx, self.tools, self.bus, self.brief)

    def _find(self, who: str | None) -> MemberSpec | None:
        if not who:
            return None
        key = who.strip().lower()
        for m in self.workers:
            if m.name.lower() == key or m.id == who:
                return m
        for m in self.workers:
            if m.role == key:
                return m
        return None

    def _round_robin(self, i: int) -> MemberSpec:
        pool = [m for m in self.workers if m.role != "chair"] or self.workers
        return pool[i % len(pool)]

    # ------------------------------------------------------------ plan
    async def plan(self) -> list[Step]:
        roles = sorted({m.role for m in self.workers})
        prompt = (
            "Plan the council's work on the topic. Respond with ONLY a JSON object: "
            '{"steps": [{"id": "s1", "title": "...", "assignee": "<member name or role>", '
            '"instructions": "...", "depends_on": []}]}. '
            f"Use 2-8 steps. Available roles: {', '.join(roles)}. "
            f"Requested output formats: {', '.join(self.ctx.outputs) or 'markdown'}. "
            "Steps without dependencies run in parallel. Do not include steps for overseers."
        )
        outcome = await self.runner(self.chair).run_task(prompt, step_title="Planning", phase="plan")
        data = parse_json_object(outcome.text) or {}
        raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        steps: list[Step] = []
        ids: set[str] = set()
        for i, s in enumerate(raw_steps[:12]):
            if not isinstance(s, dict) or not s.get("title"):
                continue
            sid = re.sub(r"[^\w\-]", "", str(s.get("id") or f"s{i + 1}"))[:20] or f"s{i + 1}"
            if sid in ids:
                sid = f"{sid}_{i}"
            ids.add(sid)
            steps.append(
                Step(
                    sid,
                    str(s["title"])[:200],
                    str(s.get("instructions") or s["title"])[:4000],
                    self._find(s.get("assignee")) or self._round_robin(i),
                    [str(d) for d in s.get("depends_on") or [] if isinstance(d, str | int)],
                )
            )
        for st in steps:
            st.depends_on = [d for d in st.depends_on if d in ids and d != st.id]
        if not steps:
            defaults = _DEFAULT_PLANS.get(self.ctx.mode, _DEFAULT_PLANS["general"])
            prev: str | None = None
            for i, (title, role, instr) in enumerate(defaults):
                sid = f"s{i + 1}"
                steps.append(
                    Step(sid, title, instr, self._find(role) or self._round_robin(i), [prev] if prev else [])
                )
                prev = sid
        self.steps = _break_cycles(steps)
        await self._emit_plan()
        return self.steps

    async def _emit_plan(self, diff: dict[str, Any] | None = None) -> None:
        self.plan_version += 1
        await self.ctx.sink.emit(
            "run.plan",
            {
                "version": self.plan_version,
                "steps": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "assignee_member_id": s.assignee.id,
                        "status": s.status,
                        "depends_on": s.depends_on,
                    }
                    for s in self.steps
                ],
                **({"diff": diff} if diff else {}),
            },
            actor=self.chair.actor,
        )

    # ------------------------------------------------------------ execute
    async def _run_step(self, step: Step, sem: asyncio.Semaphore) -> None:
        async with sem:
            step.status = "running"
            await self.ctx.sink.emit(
                "step.status",
                {"step_id": step.id, "status": "running"},
                actor=step.assignee.actor,
                step_id=step.id,
            )
            upstream = "\n\n".join(
                f"## {d.title} ({d.assignee.name})\n{d.output}"
                for d in self.steps
                if d.id in step.depends_on and d.output
            )
            try:
                outcome: AgentOutcome = await self.runner(step.assignee).run_task(
                    step.instructions,
                    step_id=step.id,
                    step_title=step.title,
                    context=upstream[-24_000:],
                )
            except (ProviderError, NotFound) as exc:
                # A provider that keeps failing (after retries and fallbacks) or was removed fails
                # this step only: its dependants are skipped and the rest of the plan goes on.
                log.warning("orchestrator.step_provider_failed", step=step.id, error=str(exc)[:200])
                outcome = AgentOutcome(
                    "", ok=False, reason=f"provider error: {type(exc).__name__}: {str(exc)[:300]}"
                )
                await self.ctx.sink.emit(
                    "member.status",
                    {"member_id": step.assignee.id, "status": "idle"},
                    actor=step.assignee.actor,
                )
            step.output = outcome.text
            step.status = "done" if outcome.ok else "failed"
            await self.ctx.sink.emit(
                "step.status",
                {
                    "step_id": step.id,
                    "status": step.status,
                    "summary": outcome.text[:500],
                    "reason": outcome.reason,
                },
                actor=step.assignee.actor,
                step_id=step.id,
            )

    async def execute(self) -> None:
        sem = asyncio.Semaphore(self.ctx.engine.rails.max_parallel_agents)
        pending = {s.id: s for s in self.steps}
        running: dict[str, asyncio.Task[None]] = {}
        finished: set[str] = set()
        try:
            while pending or running:
                for sid, st in list(pending.items()):
                    if all(d in finished for d in st.depends_on):
                        failed_dep = any(self._step(d).status in ("failed", "skipped") for d in st.depends_on)
                        if failed_dep:
                            st.status = "skipped"
                            await self.ctx.sink.emit(
                                "step.status",
                                {"step_id": sid, "status": "skipped", "summary": "dependency failed"},
                                step_id=sid,
                            )
                            finished.add(sid)
                            pending.pop(sid)
                            continue
                        running[sid] = asyncio.create_task(self._run_step(st, sem), name=f"step-{sid}")
                        pending.pop(sid)
                if not running:
                    break
                done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    sid = next(k for k, v in running.items() if v is task)
                    running.pop(sid)
                    finished.add(sid)
                    task.result()  # propagate cancellation / guardrail halts
        finally:
            for t in running.values():
                t.cancel()
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)

    def _step(self, sid: str) -> Step:
        return next(s for s in self.steps if s.id == sid)

    # ------------------------------------------------------------ synthesise & vote
    def _work_so_far(self) -> str:
        return "\n\n".join(
            f"## {s.title} — {s.assignee.name} [{s.status}]\n{s.output}" for s in self.steps if s.output
        )[-60_000:]

    async def synthesise(self, critiques: str = "") -> str:
        fmt = ", ".join(self.ctx.outputs) or "markdown"
        task = (
            f"Produce the final deliverable for the user in Markdown (it will be exported to: {fmt}). "
            "Integrate the council's work below into one complete, well-structured result. Keep citations. "
            "Return ONLY the deliverable."
            + (f"\n\nAddress these review critiques:\n{critiques}" if critiques else "")
        )
        outcome = await self.runner(self.writer).run_task(
            task, step_title="Final deliverable", context=self._work_so_far(), phase="synthesize"
        )
        return outcome.text

    async def vote(self, deliverable: str) -> tuple[bool, str]:
        voters = [
            m for m in self.members if m.role in ("critic", "chair", "analyst", "security", "tester")
        ] or [self.chair]
        vote_id = f"vote_{uuid.uuid4().hex[:10]}"
        await self.ctx.sink.emit(
            "vote.opened",
            {
                "vote_id": vote_id,
                "motion": "Accept the deliverable as final?",
                "options": [{"id": "approve", "label": "Approve"}, {"id": "revise", "label": "Revise"}],
                "quorum": len(voters) // 2 + 1,
            },
            actor=self.chair.actor,
        )

        async def cast(m: MemberSpec) -> tuple[str, str]:
            task = (
                "Review the deliverable below against the topic and the user's description. "
                "Respond with ONLY "
                '{"option": "approve"|"revise", "rationale": "<one paragraph; concrete fixes if revise>"}.'
                f"\n\nDELIVERABLE:\n{deliverable[:30_000]}"
            )
            out = await self.runner(m).run_task(task, step_title="Review vote", phase="vote")
            data = parse_json_object(out.text) or {}
            option = "revise" if str(data.get("option", "approve")).lower() == "revise" else "approve"
            rationale = str(data.get("rationale") or out.text)[:2000]
            await self.ctx.sink.emit(
                "vote.cast",
                {"vote_id": vote_id, "member_id": m.id, "option_id": option, "rationale": rationale},
                actor=m.actor,
            )
            return option, f"[{m.name}] {rationale}"

        tasks = [asyncio.create_task(cast(m), name=f"vote-{m.id}") for m in voters]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            # One voter failing (or the run being cancelled) must stop the others: they would
            # otherwise keep calling the LLM and tools after the run has ended.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        tally = {
            "approve": sum(1 for o, _ in results if o == "approve"),
            "revise": sum(1 for o, _ in results if o == "revise"),
        }
        approved = tally["approve"] >= tally["revise"]
        await self.ctx.sink.emit(
            "vote.closed",
            {"vote_id": vote_id, "result_option_id": "approve" if approved else "revise", "tally": tally},
            actor=self.chair.actor,
        )
        critiques = "\n".join(r for o, r in results if o == "revise")
        return approved, critiques

    # ------------------------------------------------------------ run
    async def run(self, max_revisions: int = 1) -> str:
        await self.plan()
        await self.execute()
        deliverable = await self.synthesise()
        for _ in range(max_revisions):
            approved, critiques = await self.vote(deliverable)
            if approved:
                break
            deliverable = await self.synthesise(critiques)
        name = DELIVERABLE_NAMES.get(self.ctx.mode, "deliverable.md")
        await self.ctx.workspace.write(
            name, deliverable, by=self.writer.name, kind="document", mime="text/markdown"
        )
        return deliverable


def _break_cycles(steps: list[Step]) -> list[Step]:
    """Drop dependency edges that would create cycles (keeps plan order as tie-breaker)."""
    order = {s.id: i for i, s in enumerate(steps)}
    for s in steps:
        s.depends_on = [d for d in s.depends_on if order[d] < order[s.id]]
    return steps
