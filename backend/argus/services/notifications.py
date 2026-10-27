"""In-app notifications: rows a user sees in the header bell when their runs finish or need approval."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.core.logging import get_logger
from argus.db.models import Notification, Run

log = get_logger(__name__)

KINDS = ("run.completed", "run.failed", "run.halted", "approval.requested")

_TITLES = {
    "run.completed": "Run completed",
    "run.failed": "Run failed",
    "run.halted": "Run halted by a guardrail",
    "approval.requested": "Approval needed",
}


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render(kind: str, topic: str, data: dict[str, Any]) -> tuple[str, str]:
    title = f"{_TITLES[kind]}: {_clip(topic, 200)}" if topic else _TITLES[kind]
    if kind == "approval.requested":
        body = str(data.get("summary") or "A council member is waiting for your decision.")
    elif kind == "run.completed":
        usage = data.get("usage") or {}
        body = f"Finished in {usage.get('steps', 0)} steps." if usage else "The council finished."
    else:
        body = str(data.get("error") or "")
    return title[:300], _clip(body, 2000)


class Notifier:
    """Turns RunManager notify events into Notification rows without blocking the run.

    ``tasks`` may be shared with another dispatcher (the webhook one) so a single drain covers both.
    """

    def __init__(
        self, sessionmaker: async_sessionmaker, tasks: set[asyncio.Task[None]] | None = None
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tasks: set[asyncio.Task[None]] = tasks if tasks is not None else set()

    def emit(self, user_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        if event not in KINDS:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.record(user_id, event, data))
        except RuntimeError:  # no running loop
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def record(self, user_id: uuid.UUID, kind: str, data: dict[str, Any]) -> Notification | None:
        try:
            run_id = uuid.UUID(str(data["run_id"])) if data.get("run_id") else None
        except ValueError:
            run_id = None
        try:
            async with self.sessionmaker() as s:
                topic = str(data.get("topic") or "")
                if run_id is not None and not topic:
                    topic = (await s.execute(select(Run.topic).where(Run.id == run_id))).scalar() or ""
                title, body = render(kind, topic, data)
                extra = {k: data[k] for k in ("approval_id", "status", "mode") if data.get(k) is not None}
                row = Notification(
                    user_id=user_id, kind=kind, title=title, body=body, run_id=run_id, data=extra
                )
                s.add(row)
                await s.commit()
                return row
        except Exception:
            log.exception("notifications.record_failed", kind=kind, run_id=str(run_id))
            return None

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def out(n: Notification) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "run_id": str(n.run_id) if n.run_id else None,
        "data": n.data or {},
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "created_at": n.created_at.isoformat(),
    }


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    q = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return int((await db.execute(q)).scalar() or 0)


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
