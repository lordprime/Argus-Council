"""Resuming a halted, failed or cancelled run where it stopped.

A resumed run is a new run with the old run's council, settings and knowledge. It runs the same
sequence (plan, steps, report stages, votes), but every agent task the old run already finished is
**replayed**: its recorded answer comes back at once, with no model call and no step counted. Only
the work that never finished calls a model. Citations, notes and workspace files carry over too.

Each finished task leaves a durable ``task.completed`` event naming the task (phase, step, title,
member) and the ``thought.completed`` event holding its answer. Runs from before those events
existed are recovered from their plan and step events instead: the plan and every finished step are
replayed, and the report is built again.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.db.models import Artifact, RunEvent
from argus.runtime.oversight import parse_json_object

TASK_EVENT = "task.completed"
RESUMABLE_STATUSES = frozenset({"halted", "failed", "cancelled"})
# Event types read from the old run (never the deltas or the whole log).
_READ = (
    "task.completed",
    "thought.completed",
    "citation.added",
    "run.plan",
    "step.status",
    "tool.call.started",
)


def task_key(phase: str, step_id: str | None, step_title: str, member_id: str) -> str:
    """A task's identity across the old run and its resumption."""
    return f"{phase}|{step_id or ''}|{step_title}|{member_id}"


@dataclass
class ResumeState:
    """What a resumed run starts from."""

    from_run_id: uuid.UUID
    # task key -> the answers the old run gave it, in order (a task can repeat, e.g. review rounds)
    answers: dict[str, deque[str]] = field(default_factory=lambda: defaultdict(deque))
    sources: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    legacy: bool = False  # recovered from plan and step events (a run from before task records)
    replayed: int = 0

    @property
    def tasks(self) -> int:
        return sum(len(q) for q in self.answers.values())

    def take(self, key: str) -> str | None:
        """The old run's answer to this task, once; None when it must run for real."""
        q = self.answers.get(key)
        if not q:
            return None
        self.replayed += 1
        return q.popleft()


async def load_resume_state(sessionmaker: async_sessionmaker, run_id: uuid.UUID) -> ResumeState:
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(RunEvent.payload)
                .where(RunEvent.run_id == run_id, RunEvent.type.in_(_READ))
                .order_by(RunEvent.seq)
            )
        ).all()
    events = [r[0] for r in rows]
    state = ResumeState(from_run_id=run_id)
    thoughts = {e.get("id"): e for e in events if e.get("type") == "thought.completed"}
    tasks = [e for e in events if e.get("type") == TASK_EVENT]
    if tasks:
        for e in tasks:
            d = e.get("data") or {}
            text = d.get("text") or (thoughts.get(d.get("thought_event_id")) or {}).get("data", {}).get(
                "text"
            )
            if d.get("ok") and isinstance(text, str) and text:
                state.answers[d["key"]].append(text)
    else:
        _recover_legacy(state, events)
    seen: set[str] = set()
    for e in events:
        if e.get("type") == "citation.added":
            d = dict(e.get("data") or {})
            if d.get("url") and d["url"] not in seen:
                seen.add(d["url"])
                state.sources.append(d)
    state.notes = _notes(events)
    return state


def _recover_legacy(state: ResumeState, events: list[dict[str, Any]]) -> None:
    """Rebuild the plan and finished steps of a run from before ``task.completed`` events."""
    state.legacy = True
    plans = [e for e in events if e.get("type") == "run.plan"]
    if not plans:
        return
    steps = {s["id"]: s for s in (plans[-1].get("data") or {}).get("steps", []) if s.get("id")}
    first_step = next((e.get("seq", 0) for e in events if e.get("type") == "step.status"), None)
    # The plan: the chair's last answer before any step started that parses as a plan.
    plan_thought = None
    for e in events:
        if first_step is not None and e.get("seq", 0) >= first_step:
            break
        d = e.get("data") or {}
        if e.get("type") == "thought.completed" and not e.get("step_id") and not d.get("tool_calls"):
            parsed = parse_json_object(d.get("text") or "")
            if parsed and isinstance(parsed.get("steps"), list):
                plan_thought = e
    if plan_thought is None:
        return
    chair = (plan_thought.get("actor") or {}).get("member_id") or ""
    state.answers[task_key("plan", None, "Planning", chair)].append(plan_thought["data"]["text"])
    # Each finished step: its last answer without tool calls before it was marked done.
    last_answer: dict[str, str] = {}
    for e in events:
        d = e.get("data") or {}
        sid = e.get("step_id")
        if e.get("type") == "thought.completed" and sid and not d.get("tool_calls") and d.get("text"):
            last_answer[sid] = d["text"]
        elif e.get("type") == "step.status" and d.get("status") == "done" and d.get("step_id") in steps:
            step = steps[d["step_id"]]
            text = last_answer.get(d["step_id"])
            if text:
                key = task_key(
                    "execute", step["id"], step.get("title", ""), step.get("assignee_member_id", "")
                )
                state.answers[key].append(text)


def _notes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notes = []
    for e in events:
        d = e.get("data") or {}
        if (
            e.get("type") == "tool.call.started"
            and d.get("tool") == "notes_write"
            and isinstance(d.get("args"), dict)
        ):
            a = d["args"]
            notes.append(
                {
                    "id": len(notes) + 1,
                    "title": str(a.get("title", ""))[:200],
                    "body": str(a.get("body", "")),
                    "source": a.get("source"),
                    "tags": a.get("tags") if isinstance(a.get("tags"), list) else [],
                    "author": (e.get("actor") or {}).get("name") or "council",
                    "ts": e.get("ts"),
                }
            )
    return notes


async def copy_workspace(sessionmaker: async_sessionmaker, from_run: uuid.UUID, workspace: Any) -> int:
    """Copy the latest version of every text file of the old run into the new run's workspace.

    Exports (binary files) are left behind: the resumed run produces its own.
    """
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(Artifact)
                .where(Artifact.run_id == from_run, Artifact.content.is_not(None))
                .order_by(Artifact.path, Artifact.version)
            )
        ).scalars()
        latest: dict[str, Artifact] = {}
        for a in rows:
            latest[a.path] = a
    copied = 0
    for a in latest.values():
        if a.deleted or a.content is None:
            continue
        await workspace.write(a.path, a.content, by=a.created_by or "resumed", kind=a.kind, mime=a.mime)
        copied += 1
    return copied
