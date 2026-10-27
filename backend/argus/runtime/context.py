"""Per-run shared state: workspace, notes, approvals, controls and interventions."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.metrics import APPROVALS, VIOLATIONS
from argus.db.models import Artifact
from argus.runtime.events import Actor, EventSink
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Violation
from argus.services.llm import LLMRouter, UsageLedger


class RunCancelled(Exception):
    pass


class Workspace:
    """Versioned text artifacts for a run, persisted in the database."""

    def __init__(
        self,
        run_id: uuid.UUID,
        sessionmaker: async_sessionmaker,
        sink: EventSink,
        engine: GuardrailEngine,
        blob_root: Path | None = None,
    ) -> None:
        self.blob_root = blob_root or Path("./data/artifacts")
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

    async def write_binary(
        self, path: str, data: bytes, *, by: str, mime: str, kind: str = "export"
    ) -> Artifact:
        rel = self.engine.normalize_path(path, write=True)
        digest = hashlib.sha256(data).hexdigest()
        blob_rel = f"{self.run_id}/{digest}"
        target = self.blob_root / blob_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        async with self.sessionmaker() as s:
            prev = (
                await s.execute(
                    select(Artifact.version)
                    .where(Artifact.run_id == self.run_id, Artifact.path == rel)
                    .order_by(Artifact.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            art = Artifact(
                run_id=self.run_id,
                path=rel,
                version=(prev or 0) + 1,
                kind=kind,
                mime=mime,
                size=len(data),
                sha256=digest,
                content=None,
                blob_path=blob_rel,
                created_by=by,
            )
            s.add(art)
            await s.commit()
        await self.sink.emit(
            "artifact.upserted",
            {
                "artifact_id": str(art.id),
                "kind": kind,
                "path": rel,
                "mime": mime,
                "version": art.version,
                "size": art.size,
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

    async def delete(self, path: str, *, by: str) -> bool:
        rel = self.engine.normalize_path(path, write=True)
        art = await self.read(rel)
        if art is None:
            return False
        async with self.sessionmaker() as s:
            s.add(
                Artifact(
                    run_id=self.run_id,
                    path=rel,
                    version=art.version + 1,
                    kind=art.kind,
                    mime=art.mime,
                    size=0,
                    sha256=hashlib.sha256(b"").hexdigest(),
                    content="",
                    created_by=by,
                    deleted=True,
                )
            )
            await s.commit()
        await self.sink.emit("artifact.deleted", {"artifact_id": str(art.id), "path": rel})
        return True

    def _latest(self, *columns: Any) -> Any:
        """SELECT ``columns`` of the newest version of every live path in this run, by path."""
        latest = (
            select(Artifact.path, func.max(Artifact.version).label("v"))
            .where(Artifact.run_id == self.run_id)
            .group_by(Artifact.path)
            .subquery()
        )
        return (
            select(*columns)
            .join(latest, (Artifact.path == latest.c.path) & (Artifact.version == latest.c.v))
            .where(Artifact.run_id == self.run_id, Artifact.deleted.is_(False))
            .order_by(Artifact.path)
        )

    async def text_files(self, limit_bytes: int = 32 * 1024 * 1024) -> dict[str, str]:
        """Latest content of every live text file (used to materialise the workspace in the sandbox).

        One query: called before every ``run_command`` and ``grep``.
        """
        out: dict[str, str] = {}
        total = 0
        q = self._latest(Artifact.path, Artifact.content).where(
            Artifact.kind != "export", Artifact.content.is_not(None)
        )
        async with self.sessionmaker() as s:
            rows = (await s.execute(q)).all()
        for path, content in rows:
            total += len(content.encode())
            if total > limit_bytes:
                break
            out[path] = content
        return out

    async def list(self) -> list[dict[str, Any]]:
        """Metadata of the latest version of every live file (content is not loaded)."""
        q = self._latest(Artifact.path, Artifact.version, Artifact.size, Artifact.kind, Artifact.mime)
        async with self.sessionmaker() as s:
            rows = (await s.execute(q)).all()
        return [
            {"path": path, "version": version, "size": size, "kind": kind, "mime": mime}
            for path, version, size, kind, mime in rows
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


class PauseGate(asyncio.Event):
    """The run's resume event (set = running, cleared = paused) that also times the pauses."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        super().__init__()
        self._clock = clock
        self._paused_at: float | None = None
        self._paused_total = 0.0

    def clear(self) -> None:
        if self.is_set() and self._paused_at is None:
            self._paused_at = self._clock()
        super().clear()

    def set(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None
        super().set()

    def paused_s(self) -> float:
        ongoing = self._clock() - self._paused_at if self._paused_at is not None else 0.0
        return self._paused_total + ongoing


@dataclass
class Controls:
    """Mutable run controls driven by user commands."""

    clock: Any = time.monotonic
    resume: PauseGate = field(init=False)
    cancelled: bool = False
    held_members: set[str] = field(default_factory=set)
    # Corrections waiting to be applied to a member's next thought.
    pending: dict[str, list[Correction]] = field(default_factory=lambda: defaultdict(list))
    # Standing orders persist for the rest of the run.
    standing: dict[str, list[Correction]] = field(default_factory=lambda: defaultdict(list))
    user_messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.resume = PauseGate(self.clock)
        self.resume.set()

    def paused_s(self) -> float:
        return self.resume.paused_s()


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
        blob_root: Path | None = None,
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
        self.workspace = Workspace(run_id, sessionmaker, sink, engine, blob_root)
        self.notes: list[dict[str, Any]] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self.controls = Controls()
        # Paused time does not count toward max_runtime_minutes.
        engine.paused_s = self.controls.paused_s
        self.approvals: dict[str, Approval] = {}
        self.approval_timeout_s = approval_timeout_s
        self.depth = depth
        self.children: set[uuid.UUID] = set()  # running sub-council runs (cancelled with this run)
        self.by_member: dict[str, dict[str, float]] = defaultdict(lambda: {"usd": 0.0, "tokens": 0})
        self.extra: dict[str, Any] = {}
        self.skills: dict[str, Any] = {}  # name -> SkillPackage available to this run
        # External notification hook (webhooks); set by the run manager.
        self.notify: Any = lambda event, data: None

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
        VIOLATIONS.labels(v.rule, v.action).inc()
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
        self.notify(
            "approval.requested",
            {"run_id": str(self.run_id), "approval_id": aid, "summary": summary, "risk": risk},
        )
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
        # The run's runtime limit keeps running while it waits: never wait past it.
        remaining = self.engine.rails.max_runtime_minutes * 60 - self.engine.runtime_s
        wait_s = max(min(self.approval_timeout_s, remaining), 0.0)
        try:
            decision = await asyncio.wait_for(fut, timeout=wait_s)
        except TimeoutError:
            reason = "approval timed out" if self.approval_timeout_s <= remaining else "runtime limit reached"
            decision = {"decision": "expired", "reason": reason}
        finally:
            self.approvals.pop(aid, None)
        APPROVALS.labels(str(decision.get("decision", "unknown"))).inc()
        await self.sink.emit("approval.resolved", {"approval_id": aid, **decision}, actor=Actor.user())
        return decision

    def resolve_approval(self, approval_id: str, decision: dict[str, Any]) -> bool:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.future.done():
            return False
        approval.future.set_result(decision)
        return True

    def expire_approvals(self, reason: str) -> None:
        """Resolve every open approval as expired (cancel, run end) so no agent stays parked."""
        for approval in list(self.approvals.values()):
            if not approval.future.done():
                approval.future.set_result({"decision": "expired", "reason": reason})

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
