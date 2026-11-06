"""Council orchestration: plan → execute (parallel, dependency-aware) → synthesise → vote → deliver."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from argus.core.errors import NotFound
from argus.core.logging import get_logger
from argus.providers.base import ProviderError
from argus.runtime.agent import AgentOutcome, AgentRunner, MemberSpec
from argus.runtime.context import RunCancelled, RunContext
from argus.runtime.council_bus import CouncilBus
from argus.runtime.debate import Debate
from argus.runtime.graph import UPSTREAM_CHARS, graph_for
from argus.runtime.guardrails import GuardrailBreach, ReportPolicy
from argus.runtime.oversight import OverseerAgent, ThoughtBus, parse_json_object
from argus.services.autocouncil import choose_assignee
from argus.tools.base import ToolRegistry
from argus.tools.council import council_tools, debate_tools
from argus.tools.spawn import MAX_STEPS, HelperPool, run_slot, spawn_tools

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
        # Members talk to each other over the council's A2A bus (argus.runtime.council_bus).
        for tool in council_tools():
            if tool.name not in tools:
                tools.register(tool)
        self.council = CouncilBus(ctx, self)
        ctx.extra["council_bus"] = self.council
        # The debate (argus.runtime.debate): the claim ledger's tools, only for a council that debates.
        if self.council.debating:
            for tool in debate_tools():
                if tool.name not in tools:
                    tools.register(tool)
        self.debate = Debate(self)
        self.plan_version = 0
        self.load: Counter[str] = Counter()  # steps given to each member, for balancing
        # Runtime helpers and a plan that grows while the council works (argus.tools.spawn). Steps and
        # helpers share one parallelism semaphore.
        for tool in spawn_tools():
            if tool.name not in tools:
                tools.register(tool)
        self.sem = asyncio.Semaphore(ctx.engine.rails.max_parallel_agents)
        self.helpers = HelperPool(self)
        self._executing = False
        self._steps_changed = asyncio.Event()
        self.added_steps = 0

    # ------------------------------------------------------------ members joining while it works
    def add_member(self, member: MemberSpec) -> None:
        """A helper joins: it is in the roster (and the live run's, the same list) until it leaves."""
        if all(m.id != member.id for m in self.members):
            self.members.append(member)

    def remove_member(self, member_id: str) -> None:
        for m in [m for m in self.members if m.id == member_id and m.ephemeral]:
            self.members.remove(m)

    def runner(self, member: MemberSpec, seat: str | None = None) -> AgentRunner:
        return AgentRunner(member, self.ctx, self.tools, self.bus, self.brief, seat=seat)

    def _find(self, who: str | None) -> MemberSpec | None:
        """The member a plan names, by name, id or role (the least loaded one when a role repeats)."""
        if not who:
            return None
        key = who.strip().lower()
        for m in self.workers:
            if m.name.lower() == key or m.id == who:
                return m
        same_role = [m for m in self.workers if m.role == key]
        if same_role:
            return min(same_role, key=lambda m: (self.load[m.id], same_role.index(m)))
        return None

    def _assign(self, who: str | None, title: str, instructions: str) -> MemberSpec:
        """Resolve a step's assignee; unnamed or unknown ones go to the best fit for the work."""
        member = self._find(who) or choose_assignee(
            f"{title}\n{instructions}",
            [m for m in self.workers if m.role != "chair"] or self.workers,
            self.load,
            budget_usd=self.ctx.engine.rails.budget.usd,
        )
        self.load[member.id] += 1
        return member

    # ------------------------------------------------------------ plan
    async def plan(self) -> list[Step]:
        roles = sorted({m.role for m in self.workers})
        prompt = (
            "Plan the council's work on the topic. Respond with ONLY a JSON object: "
            '{"steps": [{"id": "s1", "title": "...", "assignee": "<member name or role>", '
            '"instructions": "...", "depends_on": []}]}. '
            f"Use 2-8 steps. Available roles: {', '.join(roles)}. "
            f"Requested output formats: {', '.join(self.ctx.outputs) or 'markdown'}. "
            "Steps without dependencies run in parallel, so make steps independent (empty depends_on) unless "
            "a step truly needs another step's output. Do not include steps for overseers."
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
            title, instructions = str(s["title"])[:200], str(s.get("instructions") or s["title"])[:4000]
            steps.append(
                Step(
                    sid,
                    title,
                    instructions,
                    self._assign(s.get("assignee"), title, instructions),
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
                assignee = self._assign(role, title, instr)
                steps.append(Step(sid, title, instr, assignee, [prev] if prev else []))
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
    async def _upstream(self, step: Step) -> str:
        """What a step gets from the steps it depends on.

        With the council graph on (guardrails.graph): a digest per dependency (its summary, claims
        and key points, with node ids for graph_read) instead of the raw text; ``context.saved``
        records the chars this avoids. Off: the raw outputs, as before (the last 24k chars).
        """
        deps = [d for d in self.steps if d.id in step.depends_on and d.output]
        raw = "\n\n".join(f"## {d.title} ({d.assignee.name})\n{d.output}" for d in deps)[-UPSTREAM_CHARS:]
        graph = graph_for(self.ctx, self.tools)
        if graph is None:
            return raw
        await graph.ready()
        sent = graph.upstream_digest(deps)
        board_raw, board_sent = self.council.board_text_raw(), self.council.board_text()
        await graph.context_saved(
            step.assignee, step.title, len(raw) + len(board_raw), len(sent) + len(board_sent)
        )
        return sent

    async def _run_step(self, step: Step, sem: asyncio.Semaphore) -> None:
        async with run_slot(sem):  # the slot is given back while it waits on its helpers
            step.status = "running"
            await self.ctx.sink.emit(
                "step.status",
                {"step_id": step.id, "status": "running"},
                actor=step.assignee.actor,
                step_id=step.id,
            )
            upstream = await self._upstream(step)
            try:
                outcome: AgentOutcome = await self.runner(step.assignee).run_task(
                    step.instructions,
                    step_id=step.id,
                    step_title=step.title,
                    context=upstream,
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
            if outcome.ok and outcome.text:
                # On the board, so members working in parallel see it at their next turn.
                await self.council.post_board(
                    step.assignee, f"Step done: {step.title}", outcome.text[:1_500], step_id=step.id
                )
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
        """Run every step whose dependencies are done, in parallel, until the plan is worked through.

        Steps the chair adds while the council works (``add_step``) start as soon as they are ready,
        and the phase ends only when the helpers still working have finished too.
        """
        sem = self.sem
        pending = {s.id: s for s in self.steps}
        known = set(pending)
        running: dict[str, asyncio.Task[None]] = {}
        finished: set[str] = set()
        self._executing = True
        try:
            await self._restore_added_steps()
            while True:
                self._steps_changed.clear()
                for st in self.steps:
                    if st.id not in known:
                        known.add(st.id)
                        pending[st.id] = st
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
                helpers = self.helpers.running_tasks()
                if not running and not helpers:
                    break
                waker = asyncio.ensure_future(self._steps_changed.wait())
                try:
                    done, _ = await asyncio.wait(
                        [*running.values(), *helpers, waker], return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    waker.cancel()
                for task in done:
                    sid = next((k for k, v in running.items() if v is task), None)
                    if sid is None:
                        continue  # a helper finished, or a step was added
                    running.pop(sid)
                    finished.add(sid)
                    task.result()  # propagate cancellation / guardrail halts
        finally:
            self._executing = False
            for t in running.values():
                t.cancel()
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)
            await self.helpers.cancel_all()

    async def add_step(
        self,
        title: str,
        instructions: str,
        assignee: str | None,
        depends_on: list[str],
        *,
        by: MemberSpec,
        step_id: str | None = None,
    ) -> tuple[Step, bool]:
        """Add a step to the plan while the council works. Returns (step, added); the same title and
        instructions again return the existing step. Raises ValueError when it cannot be added."""
        title, instructions = title.strip()[:200], instructions.strip()[:4000]
        same = next((s for s in self.steps if s.title == title and s.instructions == instructions), None)
        if same is not None:
            return same, False
        if not self._executing:
            raise ValueError(
                "Steps can be added only while the council works through its plan; put the work in "
                "your plan, or do it yourself."
            )
        if len(self.steps) >= MAX_STEPS:
            raise ValueError(f"The plan already has {MAX_STEPS} steps, the most it may have.")
        ids = [s.id for s in self.steps]
        deps = list(dict.fromkeys(d.strip() for d in depends_on if d.strip()))
        unknown = [d for d in deps if d not in ids]
        if unknown:
            raise ValueError(f"No step called {', '.join(unknown)}. Steps: {', '.join(ids)}.")
        if step_id is None:
            self.added_steps += 1
            step_id = f"a{self.added_steps}"
            while step_id in ids:
                self.added_steps += 1
                step_id = f"a{self.added_steps}"
        member = self._assign(assignee, title, instructions)
        step = Step(step_id, title, instructions, member, deps)
        self.steps.append(step)
        await self.ctx.sink.emit(
            "step.added",
            {
                "step": {
                    "id": step.id,
                    "title": title,
                    "assignee": member.id,
                    "depends_on": deps,
                    "instructions": instructions,  # what a resumed run restores the step from
                },
                "by": by.id,
            },
            actor=by.actor,
            step_id=step.id,
        )
        await self._emit_plan({"added": [step.id]})
        self._steps_changed.set()
        return step, True

    async def _restore_added_steps(self) -> None:
        """A resumed run starts with the steps its old run added while working (their work replays)."""
        resume = self.ctx.extra.get("resume")
        for raw in getattr(resume, "added_steps", None) or []:
            by = next((m for m in self.members if m.id == raw.get("by")), self.chair)
            assignee = next((m for m in self.workers if m.id == raw.get("assignee")), None)
            with contextlib.suppress(ValueError):
                await self.add_step(
                    str(raw.get("title") or ""),
                    str(raw.get("instructions") or raw.get("title") or ""),
                    assignee.name if assignee else None,
                    [str(d) for d in raw.get("depends_on") or []],
                    by=by,
                    step_id=str(raw.get("id") or "") or None,
                )

    def _step(self, sid: str) -> Step:
        return next(s for s in self.steps if s.id == sid)

    # ------------------------------------------------------------ synthesise & vote
    def _work_so_far(self) -> str:
        work = "\n\n".join(
            f"## {s.title} — {s.assignee.name} [{s.status}]\n{s.output}" for s in self.steps if s.output
        )
        debate = self.debate.summary()  # what the council agreed and disputed, once it has converged
        return (f"{debate}\n\n{work[-(60_000 - len(debate)) :]}" if debate else work[-60_000:]).strip()

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
    def report_policy(self) -> ReportPolicy | None:
        """The report pipeline's policy when this run delivers a report (research and general runs by
        default; never sub-councils, whose deliverable goes back to their parent as text)."""
        policy = self.ctx.engine.rails.report
        enabled = policy.enabled if policy.enabled is not None else self.ctx.mode in ("research", "general")
        return policy if enabled and self.ctx.depth == 0 else None

    async def run(self, max_revisions: int = 1) -> str:
        await self.plan()
        await self.debate.seat_devils_advocate()  # a secret pick; skipped (and recorded) without two debaters
        await self.execute()
        policy = self.report_policy()
        if policy is not None:
            return await self.run_report(policy, max_revisions)
        if self.debate.active:
            # deliberate → converge → compile → co-sign (argus.runtime.debate)
            deliverable = await self.debate.conclude(max_revisions)
        else:
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

    async def run_report(self, policy: ReportPolicy, max_revisions: int) -> str:
        """Build the structured report with the report agents, put it to the council's vote, and save
        it (``report.json``) beside its Markdown rendition (the run's result)."""
        from argus.reports.pipeline import ReportPipeline
        from argus.reports.render_text import to_markdown

        pipeline = ReportPipeline(self, policy)
        debating = self.debate.active
        if debating:
            # The council argues and converges first; the report is built from what it agreed.
            await self.debate.deliberate()
            await self.debate.converge()
        try:
            report = await pipeline.build()
        except (RunCancelled, GuardrailBreach):
            raise
        except Exception as exc:  # the report must never cost the run its result
            log.warning("orchestrator.report_failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            await self.ctx.sink.emit(
                "report.stage", {"stage": "pipeline", "status": "failed", "detail": type(exc).__name__}
            )
            report = pipeline.from_deliverable(await self.synthesise())
        if debating:
            # Co-signing is the report's vote: every author signs, amends or blocks it.
            self.debate.annotate_report(report)
            deliverable = to_markdown(report)
            rounds = max_revisions + 1
            for n in range(1, rounds + 1):
                outcome, problems, endorsed = await self.debate.cosign(deliverable, n, final=n == rounds)
                critiques = "\n".join(
                    [problems]
                    + [
                        f'[{self.debate._name(a["member_id"])}] amend: replace "{a["passage"][:500]}" with '
                        f'"{a["replacement"][:800]}" ({a["reason"][:200]})'
                        for a in endorsed
                    ]
                ).strip()
                await pipeline.checkin(
                    "council vote", "council", "approved" if outcome == "signed" else "revised", critiques
                )
                if outcome != "revise":
                    break
                report = await pipeline.revise_after_vote(critiques)
                self.debate.annotate_report(report)
                deliverable = to_markdown(report)
        else:
            deliverable = to_markdown(report)
            for _ in range(max_revisions):
                approved, critiques = await self.vote(deliverable)
                await pipeline.checkin(
                    "council vote", "council", "approved" if approved else "revised", critiques
                )
                if approved:
                    break
                report = await pipeline.revise_after_vote(critiques)
                deliverable = to_markdown(report)
        report = pipeline.finish(report)
        if debating:
            self.debate.annotate_report(report)
        deliverable = to_markdown(report)
        self.ctx.extra["report"] = report
        await self.ctx.workspace.write(
            "report.json",
            report.model_dump_json(indent=2),
            by=self.writer.name,
            kind="document",
            mime="application/json",
        )
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
