"""Runtime sub-agents: helpers that join the council while it works, and a plan that can grow.

``spawn_agents`` starts one or more *helpers*. Each helper is a council member while it lives: a
``MemberSpec`` (``ephemeral=True``, ``parent_id`` = the member that spawned it) with its own A2A
agent card, registered in the orchestrator, on the council bus and in the live run (so the roster,
the map and the user's hold, correct and message reach it). Its work is an A2A task (form
``delegate``, ``metadata.spawned: true``), recorded in the protocol ledger and on the timeline like
any delegated task, and it runs at the same time as the rest of the council through the run's
parallelism semaphore. Its result is posted to the board. ``await_agents`` collects the results of
helpers spawned with ``wait=false``.

``add_step`` lets the chair add a step to the plan while the council works; the execute loop starts
it as soon as its dependencies are done.

The parallelism semaphore is held per task (``run_slot``). A member that waits for its helpers gives
its slot back while it waits (``yield_slot``), so helpers can never starve waiting on their parent.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from argus.agents.roles import ROLES
from argus.core.logging import get_logger
from argus.runtime.context import RunCancelled
from argus.runtime.council_bus import _ref, a2a_message
from argus.runtime.guardrails import GuardrailBreach
from argus.services.llm import Route
from argus.tools.base import StrList, Tool, ToolCallContext, ToolResult

if TYPE_CHECKING:
    from argus.runtime.agent import MemberSpec
    from argus.runtime.orchestrator import CouncilOrchestrator

log = get_logger(__name__)

SPAWN_PHASE = "spawn"
RESULT_CHARS = 8_000  # each helper's result as the spawning member reads it
BOARD_CHARS = 1_500
PREVIEW_CHARS = 300
MAX_STEPS = 24  # the plan, steps added while working included


# ------------------------------------------------------------------ the parallelism slot
class Slot:
    """One permit of the run's parallelism semaphore, held by one task (a step or a helper).

    ``yielded`` gives the permit back while the holder waits on others (its helpers) and takes it
    again afterwards; concurrent waits of the same holder share one release.
    """

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self.sem = sem
        self.held = False
        self.yields = 0

    async def acquire(self) -> None:
        await self.sem.acquire()
        self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self.sem.release()

    @contextlib.asynccontextmanager
    async def yielded(self) -> AsyncIterator[None]:
        self.yields += 1
        self.release()
        try:
            yield
        finally:
            self.yields -= 1
            if self.yields == 0 and not self.held:
                await self.acquire()  # cancelled here: nothing is held, so nothing is released twice


_SLOT: contextvars.ContextVar[Slot | None] = contextvars.ContextVar("argus_parallel_slot", default=None)


@contextlib.asynccontextmanager
async def run_slot(sem: asyncio.Semaphore) -> AsyncIterator[Slot]:
    """Hold one permit of ``sem`` for the enclosed work (and let it be yielded while waiting)."""
    slot = Slot(sem)
    await slot.acquire()
    token = _SLOT.set(slot)
    try:
        yield slot
    finally:
        _SLOT.reset(token)
        slot.release()


@contextlib.asynccontextmanager
async def yield_slot() -> AsyncIterator[None]:
    """Give the current task's permit back while it waits (no-op when it holds none)."""
    slot = _SLOT.get()
    if slot is None:
        yield
        return
    async with slot.yielded():
        yield


# ------------------------------------------------------------------ models for helpers
async def _pool(ctx: Any, providers: set[str]) -> list[Any]:
    """The routes the run's owner can use on the providers the council already runs on (cached)."""
    from argus.services.autocouncil import candidates

    cached = ctx.extra.get("spawn_pool")
    if cached is None:
        try:
            async with ctx.sessionmaker() as s:
                cached = await candidates(s, ctx.llm, ctx.owner_id)
        except Exception as exc:  # the council's own routes are enough to choose from
            log.info("spawn.pool_unavailable", error=type(exc).__name__)
            cached = []
        ctx.extra["spawn_pool"] = cached
    return [c for c in cached if c.provider in providers]


async def choose_model(
    ctx: Any, role: str, parent: MemberSpec, members: list[MemberSpec], requested: str | None = None
) -> Route:
    """The route for a helper of ``role``: ``requested`` when given (``model`` or ``provider/model``),
    else autocouncil's best competence-for-cost pick for the role, under the run's model strategy and
    budget, among the providers the council already uses. Falls back to the parent's route."""
    from argus.services.autocouncil import Candidate, model_tier, pick_model

    seats = [m for m in members if not m.overseer] or [parent]
    providers = {m.provider for m in seats} | {parent.provider}
    pool = await _pool(ctx, providers)
    if requested:
        provider, sep, model = requested.strip().partition("/")
        known = providers | {c.provider for c in pool}
        if sep and provider in known and model:
            return Route(provider, model)
        return Route(parent.provider, requested.strip())
    seen = {(c.provider, c.model) for c in pool}
    for m in seats:
        if (m.provider, m.model) not in seen:
            seen.add((m.provider, m.model))
            pool.append(Candidate(m.provider, m.model, model_tier(m.model), "api_key"))
    used = Counter((m.provider, m.model) for m in seats)
    strategy = ctx.extra.get("model_strategy") or "balanced"
    if strategy not in ("quality", "balanced", "economy"):
        strategy = "balanced"
    try:
        best = pick_model(role, pool, used, strategy=strategy, budget_usd=ctx.engine.rails.budget.usd)
    except Exception as exc:
        log.info("spawn.choose_model_failed", error=type(exc).__name__)
        best = None
    return Route(best.provider, best.model) if best is not None else Route(parent.provider, parent.model)


# ------------------------------------------------------------------ the helpers of one run
@dataclass
class Helper:
    spec: MemberSpec
    parent: MemberSpec
    task_id: str
    title: str
    instructions: str
    depth: int
    step_id: str | None
    task: asyncio.Task[None] | None = None
    status: str = "working"  # working | completed | failed | cancelled
    result: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class HelperPool:
    """Every helper of a run: limits, registration, their A2A tasks and their results."""

    def __init__(self, orchestrator: CouncilOrchestrator) -> None:
        self.o = orchestrator
        self.helpers: dict[str, Helper] = {}  # by A2A task id
        self.depth: dict[str, int] = {}  # member id -> spawn depth (seats are 0)
        self.spawned = 0
        self._ids: Counter[str] = Counter()

    # ------------------------------------------------------------ state
    def live(self) -> list[Helper]:
        return [h for h in self.helpers.values() if h.task is not None and not h.task.done()]

    def running_tasks(self) -> list[asyncio.Task[None]]:
        return [h.task for h in self.live() if h.task is not None]

    def get(self, task_id: str) -> Helper | None:
        return self.helpers.get(task_id)

    # ------------------------------------------------------------ spawning
    def _member_id(self, parent: MemberSpec, role: str, instructions: str) -> str:
        """Stable across a resumed run: the same parent spawning the same work gets the same id."""
        digest = hashlib.sha256(f"{parent.id}|{role}|{instructions}".encode()).hexdigest()[:8]
        base = f"{parent.id}.h{digest}"
        self._ids[base] += 1
        return base if self._ids[base] == 1 else f"{base}-{self._ids[base]}"

    def _name(self, wanted: str | None, role: str) -> str:
        taken = {m.name.lower() for m in self.o.council.members.values()} | {
            h.spec.name.lower() for h in self.helpers.values()
        }
        base = (wanted or "").strip()[:80] or f"{role.replace('_', ' ').title()} helper {self.spawned + 1}"
        name, n = base, 2
        while name.lower() in taken:
            name, n = f"{base} {n}", n + 1
        return name

    async def _spec(self, parent: MemberSpec, entry: HelperIn) -> MemberSpec:
        from argus.runtime.agent import MemberSpec

        role = re.sub(r"[^a-z0-9_]", "_", entry.role.strip().lower()).strip("_")[:40] or "assistant"
        known = ROLES.get(role)
        route = await choose_model(self.o.ctx, role, parent, self.o.members, entry.model)
        # A helper never gets a tool its parent may not use.
        tools = list(entry.tools or (known.default_tools if known else ()) or parent.tools)
        if parent.tools:
            tools = [t for t in tools if t in parent.tools] or list(parent.tools)
        fallbacks = [Route(parent.provider, parent.model), *parent.fallbacks]
        return MemberSpec(
            id=self._member_id(parent, role, entry.instructions),
            name=self._name(entry.name, role),
            role=role,
            provider=route.provider,
            model=route.model,
            system_prompt=known.system_prompt
            if known
            else f"You are a {role.replace('_', ' ')} helping the council with one task.",
            tools=tools,
            fallbacks=[f for f in fallbacks if (f.provider, f.model) != (route.provider, route.model)],
            effort=parent.effort,
            temperature=parent.temperature,
            ephemeral=True,
            parent_id=parent.id,
        )

    async def spawn(
        self, parent: MemberSpec, entries: list[HelperIn], step_id: str | None
    ) -> tuple[list[Helper], list[str]]:
        """Start a helper for each entry the limits allow. Returns (started, reasons for the rest)."""
        policy = self.o.ctx.engine.rails.spawn
        if not policy.enabled:
            return [], ["Helpers are turned off for this run (guardrail spawn.enabled)."]
        depth = self.depth.get(parent.id, 0) + 1
        if depth > policy.max_depth:
            return [], [
                f"Not spawned: helpers may nest only {policy.max_depth} deep (spawn.max_depth). "
                "Do the work yourself."
            ]
        started: list[Helper] = []
        refused: list[str] = []
        for entry in entries:
            if self.spawned >= policy.max_agents:
                refused.append(
                    f"{entry.role}: the run's {policy.max_agents} helpers are used (spawn.max_agents)"
                )
                continue
            if len(self.live()) >= policy.max_live:
                refused.append(
                    f"{entry.role}: {policy.max_live} helpers are already working (spawn.max_live)"
                )
                continue
            spec = await self._spec(parent, entry)
            self.spawned += 1
            digest = hashlib.sha256(f"{spec.role}|{entry.instructions}".encode()).hexdigest()[:10]
            helper = Helper(
                spec=spec,
                parent=parent,
                task_id=f"task_{uuid.uuid4().hex[:12]}",
                title=f"Helper task from {parent.name} ({digest})",
                instructions=entry.instructions,
                depth=depth,
                step_id=step_id,
            )
            self.helpers[helper.task_id] = helper
            self.depth[spec.id] = depth
            await self._join(helper)
            helper.task = asyncio.create_task(self._run(helper), name=f"helper-{spec.id}")
            # Its outcome is read by whoever waits; one nobody waits for must not warn when it stops.
            helper.task.add_done_callback(lambda t: t.cancelled() or t.exception())
            started.append(helper)
        return started, refused

    async def _join(self, helper: Helper) -> None:
        spec = helper.spec
        self.o.add_member(spec)
        card = self.o.council.join(spec)
        await self.o.ctx.sink.emit(
            "member.joined",
            {
                "member": {
                    "id": spec.id,
                    "name": spec.name,
                    "role": spec.role,
                    "provider": spec.provider,
                    "model": spec.model,
                    "ephemeral": True,
                    "parent_id": spec.parent_id,
                },
                "card": card,
                "task_id": helper.task_id,
            },
            actor=helper.parent.actor,
            step_id=helper.step_id,
        )

    def _prompt(self, helper: Helper) -> str:
        p = helper.parent
        return (
            f"{p.name} ({p.role}) spawned you, {helper.spec.name}, as a helper ({helper.spec.role}). "
            "You are a member of the council until this task is done: you see the board and what "
            "your teammates are working on, and you can message, ask or challenge them.\n\n"
            f"{helper.instructions}\n\n"
            "Do the task fully, using your tools where they help, and return only the result."
        )

    async def _run(self, helper: Helper) -> None:
        """The helper's A2A task: submitted, working (once it has a parallelism slot), then done."""
        ctx, bus = self.o.ctx, self.o.council
        spec, parent, step_id = helper.spec, helper.parent, helper.step_id
        t0 = time.monotonic()
        task: dict[str, Any] = {
            "kind": "task",
            "id": helper.task_id,
            "contextId": str(ctx.run_id),
            "status": {"state": "submitted"},
            "history": [],
            "artifacts": [],
            "metadata": {"from": _ref(parent), "to": [_ref(spec)], "form": "delegate", "spawned": True},
        }
        bus.tasks[helper.task_id] = task
        msg = a2a_message(
            role="agent",
            text=helper.instructions,
            context_id=str(ctx.run_id),
            sender=_ref(parent),
            recipients=[_ref(spec)],
            form="delegate",
            task_id=helper.task_id,
        )
        msg["metadata"]["spawned"] = True
        task["history"].append(msg)
        try:
            await bus._record(msg, actor=parent.actor, method="tasks/send", step_id=step_id)
            async with run_slot(self.o.sem):
                await bus._task_state(task, "working", spec.actor, step_id)
                outcome = await self.o.runner(spec).run_task(
                    self._prompt(helper), step_id=step_id, step_title=helper.title, phase=SPAWN_PHASE
                )
            helper.status = "completed" if outcome.ok and outcome.text else "failed"
            helper.result = outcome.text or f"{spec.name} returned nothing ({outcome.reason})."
        except (RunCancelled, GuardrailBreach, asyncio.CancelledError):
            helper.status = "cancelled"
            helper.result = f"{spec.name} was stopped: the run is stopping."
            with contextlib.suppress(Exception):
                await self._finish(helper, task, msg, step_id, t0)
            raise
        except Exception as exc:  # a failing helper fails alone
            log.warning("spawn.helper_failed", helper=spec.name, error=str(exc)[:200])
            helper.status = "failed"
            helper.result = f"{spec.name} failed: {type(exc).__name__}: {str(exc)[:300]}"
        await self._finish(helper, task, msg, step_id, t0)

    async def _finish(
        self, helper: Helper, task: dict[str, Any], msg: dict[str, Any], step_id: str | None, t0: float
    ) -> None:
        bus, spec = self.o.council, helper.spec
        try:
            reply = a2a_message(
                role="agent",
                text=helper.result or "(no answer)",
                context_id=str(self.o.ctx.run_id),
                sender=_ref(spec),
                recipients=[_ref(helper.parent)],
                form="delegate",
                reply_to=msg["messageId"],
                task_id=helper.task_id,
            )
            reply["metadata"]["spawned"] = True
            task["history"].append(reply)
            if helper.ok:
                task["artifacts"].append(
                    {"artifactId": f"art_{uuid.uuid4().hex[:8]}", "parts": reply["parts"]}
                )
            state = {"completed": "completed", "failed": "failed"}.get(helper.status, "canceled")
            await bus._task_state(task, state, spec.actor, step_id)
            await bus._record(
                reply, actor=spec.actor, status="ok" if helper.ok else "error", step_id=step_id, t0=t0
            )
            if helper.ok:
                title = f"Helper result: {spec.name} for {helper.parent.name}"
                await bus.post_board(spec, title, helper.result[:BOARD_CHARS], step_id=step_id)
            await self.o.ctx.sink.emit(
                "member.left",
                {
                    "member_id": spec.id,
                    "status": helper.status,
                    "result_preview": helper.result[:PREVIEW_CHARS],
                },
                actor=spec.actor,
                step_id=step_id,
            )
        finally:
            self.o.remove_member(spec.id)
            bus.leave(spec.id)

    # ------------------------------------------------------------ waiting
    async def wait(self, helpers: list[Helper], within_s: float | None = None) -> None:
        """Wait for ``helpers`` (giving the caller's parallelism slot back meanwhile). A run that is
        stopping stops the wait; a waiter that is cancelled cancels the helpers it waits for."""
        tasks = [h.task for h in helpers if h.task is not None and not h.task.done()]
        if tasks:
            try:
                async with yield_slot():
                    await asyncio.wait(tasks, timeout=within_s)
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()
                raise
        for h in helpers:
            t = h.task
            if t is not None and t.done() and not t.cancelled():
                exc = t.exception()
                if isinstance(exc, RunCancelled | GuardrailBreach):
                    raise exc

    async def cancel_all(self) -> None:
        tasks = self.running_tasks()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _report(helpers: list[Helper]) -> str:
    out = []
    for h in helpers:
        if h.task is not None and not h.task.done():
            out.append(f"### {h.spec.name} ({h.spec.role}), task {h.task_id}: still working")
            continue
        text = h.result if len(h.result) <= RESULT_CHARS else h.result[:RESULT_CHARS] + "\n[truncated]"
        out.append(f"### {h.spec.name} ({h.spec.role}), task {h.task_id}: {h.status}\n{text}")
    return "\n\n".join(out)


# ------------------------------------------------------------------ tools
class HelperIn(BaseModel):
    role: str = Field(min_length=1, max_length=60, description="A council role, e.g. researcher, analyst")
    instructions: str = Field(min_length=1, max_length=12_000, description="The full, self-contained task")
    name: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=200, description="model or provider/model")
    tools: StrList | None = Field(default=None, max_length=40)


class _SpawnTool(Tool):
    source = "council"  # every seat may spawn; tools.allow / tools.deny still apply
    parallel_safe = True

    def timeout_for(self, args: BaseModel | None) -> float | None:
        return None  # bounded by the helpers' own turn and step limits

    @staticmethod
    def _council(ctx: ToolCallContext) -> tuple[Any, Any]:
        bus = ctx.run.extra.get("council_bus")
        member = bus.members.get(ctx.member_id) if bus is not None else None
        return bus, member


class SpawnAgents(_SpawnTool):
    name = "spawn_agents"
    description = (
        "Start helper agents that work on independent sub-tasks at the same time as you and the council. "
        "Each helper joins the council while it works and gets only the instructions you write, so make "
        "each task self-contained. wait=true returns every result; wait=false returns task ids at once "
        "(collect them later with await_agents) so you keep working meanwhile."
    )

    class Args(BaseModel):
        agents: list[HelperIn] = Field(min_length=1, max_length=8)
        wait: bool = True

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        bus, parent = self._council(ctx)
        pool = getattr(getattr(bus, "o", None), "helpers", None)
        if parent is None or pool is None:
            return ToolResult.error("There is no council to add helpers to in this run.")
        started, refused = await pool.spawn(parent, args.agents, ctx.step_id)
        note = ("\nNot started: " + "; ".join(refused)) if refused else ""
        if not started:
            return ToolResult.error(("No helper was started." + note).strip())
        ids = [h.task_id for h in started]
        if not args.wait:
            listing = "\n".join(f"- {h.task_id}: {h.spec.name} ({h.spec.role})" for h in started)
            return ToolResult(
                f"Started {len(started)} helper(s); collect their results with await_agents:\n"
                f"{listing}{note}",
                data={"task_ids": ids},
            )
        await pool.wait(started)
        return ToolResult(
            _report(started) + note,
            is_error=not any(h.ok for h in started),
            data={"task_ids": ids},
        )


class AwaitAgents(_SpawnTool):
    name = "await_agents"
    description = "Wait for helpers started with spawn_agents(wait=false) and return their results."

    class Args(BaseModel):
        task_ids: StrList = Field(min_length=1, max_length=20)
        timeout_s: float = Field(default=300, ge=1, le=3_600)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        bus, member = self._council(ctx)
        pool = getattr(getattr(bus, "o", None), "helpers", None)
        if member is None or pool is None:
            return ToolResult.error("There is no council in this run.")
        found = [h for tid in dict.fromkeys(args.task_ids) if (h := pool.get(tid.strip())) is not None]
        unknown = [t for t in args.task_ids if pool.get(t.strip()) is None]
        if not found:
            return ToolResult.error(f"No helper task called {', '.join(unknown)}.")
        await pool.wait(found, within_s=args.timeout_s)
        note = f"\nUnknown task ids: {', '.join(unknown)}." if unknown else ""
        return ToolResult(_report(found) + note, data={"task_ids": [h.task_id for h in found]})


class AddStep(_SpawnTool):
    name = "add_step"
    description = (
        "Chair only: add a step to the council's plan while it works. It starts as soon as the steps it "
        "depends on are done (give no dependencies unless it truly needs their output)."
    )

    class Args(BaseModel):
        title: str = Field(min_length=1, max_length=200)
        instructions: str = Field(min_length=1, max_length=4_000)
        assignee: str | None = Field(default=None, max_length=120, description="Member name or role")
        depends_on: StrList = Field(default_factory=list, max_length=12)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        bus, member = self._council(ctx)
        o = getattr(bus, "o", None)
        if member is None or o is None or not hasattr(o, "add_step"):
            return ToolResult.error("There is no council plan in this run.")
        if member.id != o.chair.id:
            return ToolResult.error(
                f"Only the chair ({o.chair.name}) can add steps: ask them with ask_agent, or spawn a helper."
            )
        try:
            step, added = await o.add_step(
                args.title, args.instructions, args.assignee, list(args.depends_on), by=member
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        deps = f", after {', '.join(step.depends_on)}" if step.depends_on else ""
        verb = "Added" if added else "Already in the plan:"
        return ToolResult(
            f"{verb} step {step.id} '{step.title}' for {step.assignee.name}{deps}.", data={"step_id": step.id}
        )


SPAWN_TOOL_NAMES = frozenset({"spawn_agents", "await_agents", "add_step"})


def spawn_tools() -> list[Tool]:
    return [SpawnAgents(), AwaitAgents(), AddStep()]
