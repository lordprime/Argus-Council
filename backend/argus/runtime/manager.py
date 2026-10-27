"""Run manager: owns live runs in this process and applies user commands to them."""

from __future__ import annotations

import asyncio
import uuid
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.config import Settings
from argus.core.errors import ArgusError, Conflict, NotFound
from argus.core.logging import fresh_log_context, get_logger
from argus.core.metrics import RUN_DURATION, RUNS_ACTIVE, RUNS_FINISHED, RUNS_STARTED
from argus.db.models import Run, RunEvent
from argus.runtime.agent import MemberSpec
from argus.runtime.context import Correction, RunCancelled, RunContext
from argus.runtime.events import TERMINAL_STATUSES, Actor, Broker, EventSink
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Guardrails
from argus.runtime.orchestrator import CouncilOrchestrator
from argus.sandbox.client import build_sandbox
from argus.services.connections import attach_connections
from argus.services.llm import LLMRouter
from argus.services.notifications import Notifier
from argus.skills.library import skill_permitted, skills_for_owner
from argus.tools.base import ToolRegistry
from argus.tools.builtin import builtin_tools
from argus.tools.code import code_tools
from argus.tools.knowledge import knowledge_tools
from argus.tools.skills import skill_tools
from argus.tools.subcouncil import SubCouncilTool
from argus.tools.web import web_tools

log = get_logger(__name__)

ACTIVE_STATUSES = ("queued", "running", "paused")
RESTART_REASON = "Interrupted by a server restart"
# Waits between attempts to record a run's final status and event while the database is
# unavailable (about 30 s in all, on top of the event sink's own short retries).
FINALIZE_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
# A cancelled run stops at its next checkpoint; if it is stuck in a call (an LLM request, a tool)
# its task is cancelled outright after this grace period.
CANCEL_GRACE_S = 10.0


class RunStartFailed(ArgusError):
    status_code = 500
    code = "run_start_failed"


class LiveRun:
    def __init__(self, ctx: RunContext, orchestrator: CouncilOrchestrator, members: list[MemberSpec]) -> None:
        self.ctx = ctx
        self.orchestrator = orchestrator
        self.members = members
        self.task: asyncio.Task[None] | None = None
        # Set once the run starts recording its final status: commands are refused from then on.
        self.finalising = False
        self.cancel_timer: asyncio.TimerHandle | None = None


class RunManager:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        broker: Broker,
        llm: LLMRouter,
        tool_factory: Any = None,
        webhooks: Any = None,
    ) -> None:
        self.webhooks = webhooks
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.broker = broker
        self.llm = llm
        self.sandbox = build_sandbox(settings)
        self.tool_factory = tool_factory or (
            lambda run: ToolRegistry(
                [
                    *builtin_tools(),
                    *skill_tools(),
                    *knowledge_tools(),
                    *web_tools(settings),
                    *code_tools(self.sandbox),
                    SubCouncilTool(self),
                ]
            )
        )
        self.live: dict[uuid.UUID, LiveRun] = {}
        # Weak values: a lock exists only while an offline export of that run is in progress.
        self._offline_locks: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self.blob_root = Path(settings.data_dir) / "artifacts"

    def _notifier(self, owner: uuid.UUID, depth: int) -> Callable[[str, dict[str, Any]], None]:
        # In-app notifications share the webhook dispatcher's task set so one drain covers both.
        notifier = Notifier(self.sessionmaker, getattr(self.webhooks, "tasks", None))

        def _notify(event: str, data: dict[str, Any]) -> None:
            if self.webhooks is not None:
                self.webhooks.emit(owner, event, data)
            if depth == 0 or event == "approval.requested":  # sub-council results surface via the parent
                notifier.emit(owner, event, data)

        return _notify

    # ------------------------------------------------------------ lifecycle
    async def recover(self) -> None:
        """Runs left mid-flight by a previous process cannot be resumed in memory: fail them.

        Each one also gets a terminal ``run.status`` event, so the event log, snapshots and live
        streams agree with the row, plus the usual ``run.failed`` webhook and metric.
        """
        async with self.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(
                        Run.id, Run.owner_id, Run.council_id, Run.parent_run_id, Run.mode, Run.topic
                    ).where(Run.status.in_(ACTIVE_STATUSES))
                )
            ).all()
        for row in rows:
            try:
                await self._recover_one(row)
            except Exception:
                log.exception("run.recover_failed", run_id=str(row.id))

    async def _recover_one(self, row: Any) -> None:
        marked = await self._set(
            row.id,
            where=Run.status.in_(ACTIVE_STATUSES),
            status="failed",
            error=RESTART_REASON,
            finished_at=datetime.now(UTC),
        )
        if not marked:
            return
        async with self.sessionmaker() as s:
            last = (await s.execute(select(func.max(RunEvent.seq)).where(RunEvent.run_id == row.id))).scalar()
        sink = EventSink(row.id, row.council_id, self.sessionmaker, self.broker, start_seq=int(last or 0))
        await sink.emit("run.status", {"status": "failed", "reason": RESTART_REASON})
        RUNS_FINISHED.labels(row.mode, "failed").inc()
        notify = self._notifier(row.owner_id, 0 if row.parent_run_id is None else 1)
        notify(
            "run.failed",
            {
                "run_id": str(row.id),
                "topic": row.topic,
                "mode": row.mode,
                "status": "failed",
                "error": RESTART_REASON,
            },
        )

    async def start(
        self,
        run_id: uuid.UUID,
        *,
        depth: int = 0,
        listeners: list[Any] | None = None,
        budget_parent: GuardrailEngine | None = None,
    ) -> LiveRun:
        """Start a queued run.

        ``depth`` > 0 marks a sub-council, ``listeners`` observe its events and ``budget_parent``
        links its budget to the parent's. If preparing the run fails or exceeds
        ``run_start_timeout_s`` the run is marked failed (never left ``queued``) and
        ``RunStartFailed`` is raised.
        """
        async with self.sessionmaker() as s:
            run = await s.get(Run, run_id)
            if run is None:
                raise NotFound("Run not found")
            if run.status != "queued":
                raise Conflict(f"Run is {run.status}")
        sink = EventSink(run.id, run.council_id, self.sessionmaker, self.broker, start_seq=run.last_seq)
        sink.listeners.extend(listeners or [])
        closers: list[Any] = []
        try:
            live = await asyncio.wait_for(
                self._prepare(run, sink, depth, budget_parent, closers), self.settings.run_start_timeout_s
            )
        except BaseException as exc:
            await asyncio.shield(self._start_failed(run, sink, closers, depth, exc))
            if isinstance(exc, Exception):
                raise RunStartFailed("The run could not be started", details={"run_id": str(run.id)}) from exc
            raise
        self.live[run.id] = live
        live.task = asyncio.create_task(
            self._drive(live),
            name=f"run-{run.id}",
            context=fresh_log_context(
                run_id=str(run.id), owner_id=str(run.owner_id), mode=run.mode, depth=depth
            ),
        )
        return live

    async def _prepare(
        self,
        run: Run,
        sink: EventSink,
        depth: int,
        budget_parent: GuardrailEngine | None,
        closers: list[Any],
    ) -> LiveRun:
        rails = Guardrails.model_validate(run.guardrails)
        engine = GuardrailEngine(rails, parent=budget_parent)
        ctx = RunContext(
            run_id=run.id,
            owner_id=run.owner_id,
            topic=run.topic,
            description=run.description,
            mode=run.mode,
            outputs=list(run.outputs or []),
            sink=sink,
            engine=engine,
            llm=self.llm,
            sessionmaker=self.sessionmaker,
            approval_timeout_s=self.settings.approval_timeout_s,
            blob_root=self.blob_root,
            depth=depth,
        )
        ctx.notify = self._notifier(run.owner_id, depth)
        ctx.extra["closers"] = closers
        skills = await skills_for_owner(self.sessionmaker, run.owner_id)
        ctx.skills = {
            n: p for n, p in skills.items() if skill_permitted(n, rails.skills.allow, rails.skills.deny)
        }
        members = [
            MemberSpec.from_snapshot(m)
            for m in run.council_snapshot.get("members", [])
            if m.get("enabled", True)
        ]
        registry = self.tool_factory(run)
        closers.extend(
            await attach_connections(registry, self.sessionmaker, run.owner_id, self.settings, sink.emit)
            or []
        )
        orchestrator = CouncilOrchestrator(ctx, members, registry)
        return LiveRun(ctx, orchestrator, members)

    async def _start_failed(
        self, run: Run, sink: EventSink, closers: list[Any], depth: int, exc: BaseException
    ) -> None:
        if isinstance(exc, TimeoutError):
            limit = self.settings.run_start_timeout_s
            error = f"Run could not be started: preparation took longer than {limit:g}s"
        else:
            error = f"Run could not be started: {type(exc).__name__}: {str(exc)[:300]}"
        log.error("run.start_failed", run_id=str(run.id), error=error)
        for close in closers:
            try:
                await close()
            except Exception:  # noqa: S110
                pass
        try:
            marked = await self._set(
                run.id,
                where=Run.status == "queued",
                status="failed",
                error=error,
                finished_at=datetime.now(UTC),
            )
            status = "failed" if marked else "cancelled"  # the only other way out of "queued"
            await sink.emit("run.status", {"status": status, **({"reason": error} if marked else {})})
            RUNS_FINISHED.labels(run.mode, status).inc()
            if marked:
                self._notifier(run.owner_id, depth)(
                    "run.failed",
                    {
                        "run_id": str(run.id),
                        "topic": run.topic,
                        "mode": run.mode,
                        "status": status,
                        "error": error,
                    },
                )
        except Exception:
            log.exception("run.start_failed_unrecorded", run_id=str(run.id))

    async def _set(self, run_id: uuid.UUID, *, where: Any = None, **values: Any) -> bool:
        """Update a run row; with ``where`` only when it matches. Returns whether a row changed."""
        q = update(Run).where(Run.id == run_id)
        if where is not None:
            q = q.where(where)
        async with self.sessionmaker() as s:
            res = await s.execute(q.values(**values))
            await s.commit()
        return bool(res.rowcount)

    async def _drive(self, live: LiveRun) -> None:
        ctx = live.ctx
        status, error, result = "completed", None, ""
        RUNS_STARTED.labels(ctx.mode).inc()
        RUNS_ACTIVE.inc()
        try:
            try:
                # Conditional: a cancel that arrived while the run was being started wins.
                if not await self._set(
                    ctx.run_id, where=Run.status == "queued", status="running", started_at=datetime.now(UTC)
                ):
                    raise RunCancelled()
                ctx.notify("run.started", {"run_id": str(ctx.run_id), "topic": ctx.topic, "mode": ctx.mode})
                await ctx.sink.emit("run.status", {"status": "running"})
                result = await live.orchestrator.run()
                await self._auto_export(ctx, result)
            except RunCancelled:
                status = "cancelled"
            except GuardrailBreach as exc:
                status, error = "halted", exc.violation.message
                await _best_effort(ctx.violation(exc.violation), "run.violation_unrecorded")
            except asyncio.CancelledError:
                status, error = "cancelled", None if ctx.controls.cancelled else "Server shutting down"
            except Exception as exc:
                log.exception("run.crashed", run_id=str(ctx.run_id))
                status, error = "failed", f"{type(exc).__name__}: {str(exc)[:500]}"
                await _best_effort(
                    ctx.sink.emit("error", {"code": "run_failed", "message": error}), "run.error_unrecorded"
                )
            await self._finalize(live, status, error, result)
        finally:
            # Runs whatever happened above, so a run can never stay "live" with a dead task.
            if live.cancel_timer is not None:
                live.cancel_timer.cancel()
            self.live.pop(ctx.run_id, None)
            RUNS_ACTIVE.dec()
            RUNS_FINISHED.labels(ctx.mode, status).inc()
            RUN_DURATION.labels(ctx.mode, status).observe(ctx.engine.runtime_s)

    async def _finalize(self, live: LiveRun, status: str, error: str | None, result: str) -> None:
        """Record the final status. Each step is isolated; the status write and the terminal
        event are retried so a brief database outage cannot leave a run looking alive."""
        ctx = live.ctx
        live.finalising = True
        for close in ctx.extra.get("closers", []):
            await _best_effort(close(), "run.closer_failed")
        ctx.expire_approvals("run ended")
        used = ctx.engine.total_usage
        usage = {
            "usd": round(used.cost_usd, 6),
            "tokens_in": used.input_tokens,
            "tokens_out": used.output_tokens,
            "steps": ctx.engine.steps,
            "llm_calls": ctx.ledger.calls,
            "runtime_s": round(ctx.engine.runtime_s, 1),
        }
        await _best_effort(ctx.budget_update(final=True), "run.budget_unrecorded")
        await _retry(
            lambda: self._set(
                ctx.run_id,
                status=status,
                error=error,
                result=result,
                usage=usage,
                finished_at=datetime.now(UTC),
            ),
            "run.status_unrecorded",
            run_id=ctx.run_id,
        )
        try:
            # Queue the webhook as soon as the terminal status is committed.
            ctx.notify(
                f"run.{status}",
                {
                    "run_id": str(ctx.run_id),
                    "topic": ctx.topic,
                    "mode": ctx.mode,
                    "status": status,
                    "error": error,
                    "usage": usage,
                },
            )
        except Exception:
            log.exception("run.notify_failed", run_id=str(ctx.run_id))
        await _retry(
            lambda: ctx.sink.emit("run.status", {"status": status, **({"reason": error} if error else {})}),
            "run.terminal_event_unrecorded",
            run_id=ctx.run_id,
        )

    async def _auto_export(self, ctx: RunContext, markdown: str) -> None:
        from argus.services.exports import export_markdown

        meta = {"run_id": str(ctx.run_id), "mode": ctx.mode, "topic": ctx.topic}
        for fmt in ctx.outputs:
            if fmt == "md":
                continue  # the Markdown deliverable is already in the workspace
            try:
                await export_markdown(ctx.workspace, markdown, fmt, title=ctx.topic, run_meta=meta)
            except Exception as exc:
                log.warning("run.export_failed", format=fmt, error=str(exc)[:200])

    async def export(self, run_id: uuid.UUID, fmt: str) -> dict[str, Any]:
        """Export the deliverable of a live or finished run."""
        from argus.services.exports import export_markdown

        async with self.sessionmaker() as s:
            run = await s.get(Run, run_id)
        if run is None:
            raise NotFound("Run not found")
        if not run.result:
            raise Conflict("Run has no deliverable yet")
        meta = {"run_id": str(run.id), "mode": run.mode, "topic": run.topic}
        live = self.live.get(run_id)
        if live is not None:
            return await export_markdown(live.ctx.workspace, run.result, fmt, title=run.topic, run_meta=meta)
        lock = self._offline_locks.get(run_id)
        if lock is None:
            lock = self._offline_locks[run_id] = asyncio.Lock()
        async with lock:
            async with self.sessionmaker() as s:
                run = await s.get(Run, run_id)
            sink = EventSink(run.id, run.council_id, self.sessionmaker, self.broker, start_seq=run.last_seq)
            engine = GuardrailEngine(Guardrails.model_validate(run.guardrails))
            from argus.runtime.context import Workspace

            ws = Workspace(run.id, self.sessionmaker, sink, engine, self.blob_root)
            return await export_markdown(ws, run.result, fmt, title=run.topic, run_meta=meta)

    async def shutdown(self) -> None:
        tasks = [lr.task for lr in self.live.values() if lr.task]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait(self, run_id: uuid.UUID, limit_s: float = 30) -> None:
        live = self.live.get(run_id)
        if live and live.task:
            await asyncio.wait_for(asyncio.shield(live.task), limit_s)

    # ------------------------------------------------------------ commands
    def _live(self, run_id: uuid.UUID) -> LiveRun:
        live = self.live.get(run_id)
        if live is None or live.finalising or (live.task is not None and live.task.done()):
            raise Conflict("Run is not active")
        return live

    async def pause(self, run_id: uuid.UUID) -> None:
        live = self._live(run_id)
        if not await self._set(run_id, where=Run.status == "running", status="paused"):
            raise Conflict("Run is not running")
        live.ctx.controls.resume.clear()
        await live.ctx.sink.emit("run.status", {"status": "paused"}, actor=Actor.user())

    async def resume(self, run_id: uuid.UUID) -> None:
        live = self._live(run_id)
        if not await self._set(run_id, where=Run.status == "paused", status="running"):
            raise Conflict("Run is not paused")
        live.ctx.controls.resume.set()
        await live.ctx.sink.emit("run.status", {"status": "running"}, actor=Actor.user())

    def member(self, run_id: uuid.UUID, member_id: str) -> Any:
        live = self.live.get(run_id)
        return next((m for m in live.members if m.id == member_id), None) if live else None

    async def cancel(self, run_id: uuid.UUID) -> None:
        live = self.live.get(run_id)
        if live is None:
            # Not driven here (finished, or still being started): the conditional update never
            # touches a terminal run, and a starting run sees it when it tries to go "running".
            await self._set(
                run_id,
                where=Run.status.not_in(TERMINAL_STATUSES),
                status="cancelled",
                finished_at=datetime.now(UTC),
            )
            return
        if live.finalising:
            return  # already recording its final status
        ctx = live.ctx
        ctx.controls.cancelled = True
        ctx.controls.resume.set()
        ctx.expire_approvals("run cancelled")
        for child_id in list(ctx.children):
            await self.cancel(child_id)
        if live.cancel_timer is None and live.task is not None and not live.task.done():
            live.cancel_timer = asyncio.get_running_loop().call_later(
                CANCEL_GRACE_S, self._force_cancel, live
            )

    def _force_cancel(self, live: LiveRun) -> None:
        if live.task is not None and not live.task.done() and not live.finalising:
            log.warning("run.cancel_forced", run_id=str(live.ctx.run_id), grace_s=CANCEL_GRACE_S)
            live.task.cancel()

    async def resolve_approval(self, run_id: uuid.UUID, approval_id: str, decision: dict[str, Any]) -> None:
        if not self._live(run_id).ctx.resolve_approval(approval_id, decision):
            raise NotFound("Approval not found or already resolved")

    async def intervene(
        self, run_id: uuid.UUID, cmd: dict[str, Any], user_name: str = "You"
    ) -> dict[str, Any]:
        """Apply a user intervention. The user outranks every overseer."""
        live = self._live(run_id)
        ctx = live.ctx
        kind = cmd["kind"]
        target = cmd.get("target") or {}
        member_id = target.get("member_id")
        member_ids = {m.id for m in live.members}
        if member_id and member_id not in member_ids:
            raise NotFound("Member not found in this run")
        user = Actor.user(user_name)
        if kind == "message":
            ctx.controls.user_messages.append({"text": cmd["text"], "target": member_id or "council"})
            env = await ctx.sink.emit(
                "user.message", {"text": cmd["text"], "target_member_id": member_id or "council"}, actor=user
            )
            return {"event_id": env.id}
        if kind in ("intercept", "hold"):
            if not member_id:
                raise NotFound("intercept/hold needs a target member")
            ctx.controls.held_members.add(member_id)
            iid = f"int_{uuid.uuid4().hex[:12]}"
            await ctx.sink.emit(
                "intercept.raised",
                {
                    "intercept_id": iid,
                    "target": {"member_id": member_id, "thought_id": target.get("thought_id")},
                    "by": "user",
                    "reason": "user",
                    "severity": "high",
                    "action": "hold",
                    "note": cmd.get("text"),
                },
                actor=user,
            )
            await ctx.sink.emit("member.status", {"member_id": member_id, "status": "held"}, actor=user)
            if cmd.get("text"):
                await self._correct(live, member_id, cmd["text"], cmd.get("scope") or "thought", user, iid)
                await self.release(run_id, member_id)
            return {"intercept_id": iid}
        if kind == "release":
            await self.release(run_id, member_id)
            return {}
        if kind == "correct":
            scope = cmd.get("scope") or ("member" if member_id else "council")
            cid = await self._correct(live, member_id, cmd["text"], scope, user, target.get("intercept_id"))
            if member_id:
                await self.release(run_id, member_id)
            return {"correction_id": cid}
        if kind == "halt_all":
            for m in live.members:
                ctx.controls.held_members.add(m.id)
            await ctx.sink.emit(
                "intercept.raised",
                {
                    "intercept_id": f"int_{uuid.uuid4().hex[:12]}",
                    "target": {"member_id": None},
                    "by": "user",
                    "reason": "user",
                    "severity": "high",
                    "action": "hold",
                    "note": "Halt all",
                },
                actor=user,
            )
            return {}
        if kind == "release_all":
            ctx.controls.held_members.clear()
            return {}
        raise NotFound(f"Unknown intervention '{kind}'")

    async def release(self, run_id: uuid.UUID, member_id: str | None) -> None:
        live = self._live(run_id)
        if member_id:
            live.ctx.controls.held_members.discard(member_id)
            await live.ctx.sink.emit(
                "member.status", {"member_id": member_id, "status": "idle"}, actor=Actor.user()
            )

    async def _correct(
        self,
        live: LiveRun,
        member_id: str | None,
        text: str,
        scope: str,
        user: Actor,
        intercept_id: str | None,
    ) -> str:
        ctx = live.ctx
        cid = f"cor_{uuid.uuid4().hex[:12]}"
        targets = [member_id] if member_id else [m.id for m in live.members]
        for t in targets:
            corr = Correction(cid, text, scope, "user", target_member_id=t, intercept_id=intercept_id)
            if scope in ("member", "council"):
                ctx.controls.standing["council" if scope == "council" else t].append(corr)
                if scope == "council":
                    break
            else:
                ctx.controls.pending[t].append(corr)
        await ctx.sink.emit(
            "correction.issued",
            {
                "correction_id": cid,
                "intercept_id": intercept_id,
                "target_member_id": member_id or "council",
                "text": text,
                "scope": scope,
                "force": True,
            },
            actor=user,
        )
        return cid


async def _best_effort(aw: Awaitable[Any], what: str) -> None:
    try:
        await aw
    except Exception as exc:
        log.warning(what, error=f"{type(exc).__name__}: {str(exc)[:200]}")


async def _retry(fn: Callable[[], Awaitable[Any]], what: str, *, run_id: uuid.UUID) -> bool:
    """Run ``fn`` until it succeeds, waiting ``FINALIZE_DELAYS`` between attempts."""
    for attempt in range(len(FINALIZE_DELAYS) + 1):
        try:
            await fn()
            return True
        except Exception as exc:
            if attempt == len(FINALIZE_DELAYS):
                log.error(what, run_id=str(run_id), error=f"{type(exc).__name__}: {str(exc)[:200]}")
                return False
            log.warning(f"{what}_retry", run_id=str(run_id), attempt=attempt + 1, error=str(exc)[:200])
            await asyncio.sleep(FINALIZE_DELAYS[attempt])
    return False


async def list_active(sessionmaker: async_sessionmaker, owner_id: uuid.UUID) -> list[Run]:
    async with sessionmaker() as s:
        rows = await s.execute(
            select(Run).where(Run.owner_id == owner_id, Run.status.in_(("running", "paused")))
        )
        return list(rows.scalars())
