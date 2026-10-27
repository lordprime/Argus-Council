"""Run manager: owns live runs in this process and applies user commands to them."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.config import Settings
from argus.core.errors import Conflict, NotFound
from argus.core.logging import get_logger
from argus.db.models import Run
from argus.runtime.agent import MemberSpec
from argus.runtime.context import Correction, RunCancelled, RunContext
from argus.runtime.events import TERMINAL_STATUSES, Actor, Broker, EventSink
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Guardrails
from argus.runtime.orchestrator import CouncilOrchestrator
from argus.sandbox.client import build_sandbox
from argus.services.connections import attach_connections
from argus.services.llm import LLMRouter
from argus.skills.library import skill_permitted, skills_for_owner
from argus.tools.base import ToolRegistry
from argus.tools.builtin import builtin_tools
from argus.tools.code import code_tools
from argus.tools.skills import skill_tools
from argus.tools.web import web_tools

log = get_logger(__name__)


class LiveRun:
    def __init__(self, ctx: RunContext, orchestrator: CouncilOrchestrator, members: list[MemberSpec]) -> None:
        self.ctx = ctx
        self.orchestrator = orchestrator
        self.members = members
        self.task: asyncio.Task[None] | None = None


class RunManager:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        broker: Broker,
        llm: LLMRouter,
        tool_factory: Any = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.broker = broker
        self.llm = llm
        self.sandbox = build_sandbox(settings)
        self.tool_factory = tool_factory or (
            lambda run: ToolRegistry(
                [*builtin_tools(), *skill_tools(), *web_tools(settings), *code_tools(self.sandbox)]
            )
        )
        self.live: dict[uuid.UUID, LiveRun] = {}
        self._offline_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self.blob_root = Path(settings.data_dir) / "artifacts"

    # ------------------------------------------------------------ lifecycle
    async def recover(self) -> None:
        """Runs left mid-flight by a previous process cannot be resumed in memory: mark them."""
        async with self.sessionmaker() as s:
            await s.execute(
                update(Run)
                .where(Run.status.in_(("running", "paused", "queued")))
                .values(
                    status="failed", error="Interrupted by a server restart", finished_at=datetime.now(UTC)
                )
            )
            await s.commit()

    async def start(self, run_id: uuid.UUID) -> None:
        async with self.sessionmaker() as s:
            run = await s.get(Run, run_id)
            if run is None:
                raise NotFound("Run not found")
            if run.status != "queued":
                raise Conflict(f"Run is {run.status}")
        sink = EventSink(run.id, run.council_id, self.sessionmaker, self.broker, start_seq=run.last_seq)
        rails = Guardrails.model_validate(run.guardrails)
        engine = GuardrailEngine(rails)
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
            blob_root=self.blob_root,
        )
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
        ctx.extra["closers"] = await attach_connections(
            registry, self.sessionmaker, run.owner_id, self.settings, sink.emit
        )
        orchestrator = CouncilOrchestrator(ctx, members, registry)
        live = LiveRun(ctx, orchestrator, members)
        self.live[run.id] = live
        live.task = asyncio.create_task(self._drive(live), name=f"run-{run.id}")

    async def _set(self, run_id: uuid.UUID, **values: Any) -> None:
        async with self.sessionmaker() as s:
            await s.execute(update(Run).where(Run.id == run_id).values(**values))
            await s.commit()

    async def _drive(self, live: LiveRun) -> None:
        ctx = live.ctx
        status, error, result = "completed", None, ""
        await self._set(ctx.run_id, status="running", started_at=datetime.now(UTC))
        await ctx.sink.emit("run.status", {"status": "running"})
        try:
            result = await live.orchestrator.run()
            await self._auto_export(ctx, result)
        except RunCancelled:
            status = "cancelled"
        except GuardrailBreach as exc:
            status, error = "halted", exc.violation.message
            await ctx.violation(exc.violation)
        except asyncio.CancelledError:
            status, error = "cancelled", "Server shutting down"
        except Exception as exc:
            log.exception("run.crashed", run_id=str(ctx.run_id))
            status, error = "failed", f"{type(exc).__name__}: {str(exc)[:500]}"
            await ctx.sink.emit("error", {"code": "run_failed", "message": error})
        finally:
            for close in ctx.extra.get("closers", []):
                try:
                    await close()
                except Exception:  # noqa: S110
                    pass
            for approval in list(ctx.approvals.values()):
                if not approval.future.done():
                    approval.future.set_result({"decision": "expired", "reason": "run ended"})
            usage = {
                "usd": round(ctx.engine.usage.cost_usd, 6),
                "tokens_in": ctx.engine.usage.input_tokens,
                "tokens_out": ctx.engine.usage.output_tokens,
                "steps": ctx.engine.steps,
                "llm_calls": ctx.ledger.calls,
                "runtime_s": round(ctx.engine.runtime_s, 1),
            }
            await ctx.budget_update(final=True)
            await self._set(
                ctx.run_id,
                status=status,
                error=error,
                result=result,
                usage=usage,
                finished_at=datetime.now(UTC),
            )
            await ctx.sink.emit("run.status", {"status": status, **({"reason": error} if error else {})})
            self.live.pop(ctx.run_id, None)

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
        async with self._offline_locks.setdefault(run_id, asyncio.Lock()):
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
        if live is None:
            raise Conflict("Run is not active")
        return live

    async def pause(self, run_id: uuid.UUID) -> None:
        live = self._live(run_id)
        live.ctx.controls.resume.clear()
        await self._set(run_id, status="paused")
        await live.ctx.sink.emit("run.status", {"status": "paused"}, actor=Actor.user())

    async def resume(self, run_id: uuid.UUID) -> None:
        live = self._live(run_id)
        live.ctx.controls.resume.set()
        await self._set(run_id, status="running")
        await live.ctx.sink.emit("run.status", {"status": "running"}, actor=Actor.user())

    async def cancel(self, run_id: uuid.UUID) -> None:
        live = self.live.get(run_id)
        if live is None:
            async with self.sessionmaker() as s:
                run = await s.get(Run, run_id)
                if run is not None and run.status not in TERMINAL_STATUSES:
                    await self._set(run_id, status="cancelled", finished_at=datetime.now(UTC))
            return
        live.ctx.controls.cancelled = True
        live.ctx.controls.resume.set()

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


async def list_active(sessionmaker: async_sessionmaker, owner_id: uuid.UUID) -> list[Run]:
    async with sessionmaker() as s:
        rows = await s.execute(
            select(Run).where(Run.owner_id == owner_id, Run.status.in_(("running", "paused")))
        )
        return list(rows.scalars())
