from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, write_scope
from argus.core.errors import NotFound
from argus.db.models import Notification
from argus.services.notifications import mark_all_read, out, unread_count

# Marking read changes only the caller's own inbox, so viewers may do it too: reads need no scope,
# and the write scope check is skipped for the two read-marking POSTs.
router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
    dependencies=[write_scope("runs:write", exempt=("/read", "/read-all"))],
)


@router.get("")
async def list_notifications(
    principal: CurrentPrincipal,
    db: DbDep,
    unread: bool | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    before: datetime | None = Query(default=None, description="Cursor: notifications created before this"),
) -> dict:
    q = select(Notification).where(Notification.user_id == principal.id)
    if unread is True:
        q = q.where(Notification.read_at.is_(None))
    elif unread is False:
        q = q.where(Notification.read_at.is_not(None))
    if before is not None:
        q = q.where(Notification.created_at < before)
    rows = await db.execute(q.order_by(Notification.created_at.desc()).limit(limit))
    return {
        "notifications": [out(n) for n in rows.scalars()],
        "unread": await unread_count(db, principal.id),
    }


@router.get("/unread-count")
async def get_unread_count(principal: CurrentPrincipal, db: DbDep) -> dict:
    return {"unread": await unread_count(db, principal.id)}


@router.post("/read-all")
async def read_all(principal: CurrentPrincipal, db: DbDep) -> dict:
    marked = await mark_all_read(db, principal.id)
    return {"marked": marked, "unread": 0}


@router.post("/{notification_id}/read")
async def read_one(notification_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> dict:
    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != principal.id:
        raise NotFound("Notification not found")
    if n.read_at is None:
        n.read_at = datetime.now(UTC)
        await db.flush()
    return out(n)
