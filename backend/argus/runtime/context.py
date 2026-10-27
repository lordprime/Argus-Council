"""Per-run shared state: workspace, notes, approvals, controls and interventions."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.db.models import Artifact
from argus.runtime.events import Actor, EventSink
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Violation
from argus.services.llm import LLMRouter, UsageLedger


class RunCancelled(Exception):
    pass


class Workspace:
    """Versioned text artifacts for a run, persisted in the database."""

    def __init__(
        self, run_id: uuid.UUID, sessionmaker: async_sessionmaker, sink: EventSink, engine: GuardrailEngine
    ) -> None:
        self.run_id = run_id
        self.sessionmaker = sessionmaker
        self.sink = sink
        self.engine = engine

    async def write(
        self, path: str, content: str, *, by: str, kind: str = "file", mime: str | None = None
    ) -> Artifact:
        rel = self.engine.normalize_path(path, write=True)
        data = content.encode()
        v = self.engine.check_file_size(len(data))
        if v:
            raise GuardrailBreach(v)
        async with self.sessionmaker() as s:
            prev = (
                await s.execute(
                    select(Artifact)
                    .where(Artifact.run_id == self.run_id, Artifact.path == rel)
                    .order_by(Artifact.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            art = Artifact(
                run_id=self.run_id,
                path=rel,
                version=(prev.version + 1) if prev else 1,
                kind=kind,
                mime=mime or mimetypes.guess_type(rel)[0] or "text/plain",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                content=content,
                created_by=by,
            )
            s.add(art)
            await s.commit()
        added, removed = _diff_stats(prev.content if prev and not prev.deleted else "", content)
        await self.sink.emit(
            "artifact.upserted",
            {
                "artifact_id": str(art.id),
                "kind": kind,
                "path": rel,
                "mime": art.mime,
                "version": art.version,
                "size": art.size,
                "diff_stats": {"added": added, "removed": removed},
            },
        )
        return art

    async def read(self, path: str, version: int | None = None) -> Artifact | None:
        rel = self.engine.normalize_path(path, write=False)
        async with self.sessionmaker() as s:
            q = select(Artifact).where(Artifact.run_id == self.run_id, Artifact.path == rel)
            q = (
                q.where(Artifact.version == version)
                if version
                else q.order_by(Artifact.version.desc()).limit(1)
            )
            art = (await s.execute(q)).scalar_one_or_none()
        return None if art is None or art.deleted else art

    async def list(self) -> list[dict[str, Any]]:
        async with self.sessionmaker() as s:
            latest = (
                select(Artifact.path, func.max(Artifact.version).label("v"))
                .where(Artifact.run_id == self.run_id)
                .group_by(Artifact.path)
                .subquery()
            )
            rows = await s.execute(
                select(Artifact)
                .join(latest, (Artifact.path == latest.c.path) & (Artifact.version == latest.c.v))
                .where(Artifact.run_id == self.run_id)
                .order_by(Artifact.path)
            )
            return [
                {"path": a.path, "version": a.version, "size": a.size, "kind": a.kind, "mime": a.mime}
                for a in rows.scalars()
                if not a.deleted
            ]


def _diff_stats(old: str | None, new: str) -> tuple[int, int]:
    import difflib

    added = removed = 0
    for line in difflib.ndiff((old or "").splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


@dataclass
class Approval:
    id: str
    member_id: str
    summary: str
    risk: list[str]
    future: asyncio.Future[dict[str, Any]]


@dataclass
class Correction:
    id: str
    text: str
    scope: str  # thought | member | council
    by: str  # user | overseer role
    target_member_id: str | None = None
    intercept_id: str | None = None


@dataclass
class Controls:
    """Mutable run controls driven by user commands."""

    resume: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    held_members: set[str] = field(default_factory=set)
    # Corrections waiting to be applied to a member's next thought.
    pending: dict[str, list[Correction]] = field(default_factory=lambda: defaultdict(list))
    # Standing orders persist for the rest of the run.
    standing: dict[str, list[Correction]] = field(default_factory=lambda: defaultdict(list))
    user_messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.resume.set()


class RunContext:
    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        owner_id: uuid.UUID,
        topic: str,
        description: str,
        mode: str,
        outputs: list[str],
        sink: EventSink,
        engine: GuardrailEngine,
        llm: LLMRouter,
        sessionmaker: async_sessionmaker,
        approval_timeout_s: float = 3600,
        depth: int = 0,
    ) -> None:
        self.run_id = run_id
        self.owner_id = owner_id
        self.topic = topic
        self.description = description
        self.mode = mode
        self.outputs = outputs
        self.sink = sink
        self.engine = engine
        self.llm = llm
        self.sessionmaker = sessionmaker
        self.ledger = UsageLedger()
        self.workspace = Workspace(run_id, sessionmaker, sink, engine)
        self.notes: list[dict[str, Any]] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self.controls = Controls()
        self.approvals: dict[str, Approval] = {}
        self.approval_timeout_s = approval_timeout_s
        self.depth = depth
        self.by_member: dict[str, dict[str, float]] = defaultdict(lambda: {"usd": 0.0, "tokens": 0})
        self.extra: dict[str, Any] = {}

    # -------------------------------------------------- control flow
    async def checkpoint(self, member_id: str | None = None) -> None:
        """Called between actions: honours cancel, pause, holds and limits."""
        while True:
            if self.controls.cancelled:
                raise RunCancelled()
            await self.controls.resume.wait()
            if self.controls.cancelled:
                raise RunCancelled()
            if member_id is None or member_id not in self.controls.held_members:
                break
            await asyncio.sleep(0.05)
        self.engine.enforce_limits()

    async def violation(self, v: Violation, target: dict[str, Any] | None = None, actor: Actor | None = None):
        await self.sink.emit("guardrail.violation", v.as_event(target), actor=actor)

    async def budget_update(self, *, final: bool = False) -> None:
        snap = self.engine.budget_snapshot()
        snap["by_member"] = {k: dict(v) for k, v in self.by_member.items()}
        if final:
            snap["final"] = True
        await self.sink.emit("budget.update", snap, durable=final)

    # -------------------------------------------------- approvals
    async def request_approval(
        self, member: Actor, summary: str, risk: list[str], tool_call_id: str | None = None
    ) -> dict[str, Any]:
        aid = f"apr_{uuid.uuid4().hex[:12]}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.approvals[aid] = Approval(aid, member.member_id or "", summary, risk, fut)
        await self.sink.emit(
            "approval.requested",
            {
                "approval_id": aid,
                "tool_call_id": tool_call_id,
                "member_id": member.member_id,
                "action_summary": summary,
                "risk": risk,
                "rule": "approval_required",
            },
            actor=member,
        )
        try:
            decision = await asyncio.wait_for(fut, timeout=self.approval_timeout_s)
        except TimeoutError:
            decision = {"decision": "expired"}
        finally:
            self.approvals.pop(aid, None)
        await self.sink.emit("approval.resolved", {"approval_id": aid, **decision}, actor=Actor.user())
        return decision

    def resolve_approval(self, approval_id: str, decision: dict[str, Any]) -> bool:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.future.done():
            return False
        approval.future.set_result(decision)
        return True

    # -------------------------------------------------- corrections
    def corrections_for(self, member_id: str) -> tuple[list[Correction], list[Correction]]:
        """Return ``(one_shot, standing)`` corrections that apply to a member."""
        # Council-wide one-shot corrections are fanned out per member when issued.
        one_shot = self.controls.pending.pop(member_id, [])
        standing = self.controls.standing.get(member_id, []) + self.controls.standing.get("council", [])
        return one_shot, standing

    def corrections_for_peek(self, member_id: str) -> tuple[list[Correction], list[Correction]]:
        """Like ``corrections_for`` but without consuming one-shot corrections."""
        return (
            list(self.controls.pending.get(member_id, [])),
            self.controls.standing.get(member_id, []) + self.controls.standing.get("council", []),
        )
