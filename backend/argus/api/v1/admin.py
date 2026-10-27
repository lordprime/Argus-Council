"""Admin-only user management (RBAC). Mutations are recorded by the audit middleware."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from argus.api.deps import AdminPrincipal, AdminWriter, DbDep
from argus.core.errors import Conflict, NotFound
from argus.db.models import Run, User
from argus.services.connections import disable_stdio_servers

router = APIRouter(prefix="/admin", tags=["admin"])

Role = Literal["admin", "member", "viewer"]


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: Role
    is_admin: bool
    is_active: bool
    mfa_enabled: bool = False
    created_at: datetime
    last_login_at: datetime | None
    run_count: int


class AdminUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: bool | None = None
    # Turn off a user's two-factor authentication (lost device and recovery codes).
    reset_mfa: bool | None = None


def _out(user: User, run_count: int) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,  # type: ignore[arg-type]
        is_admin=user.is_admin,
        is_active=user.is_active,
        mfa_enabled=bool(user.totp_enabled),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        run_count=run_count,
    )


async def _run_count(db: DbDep, user_id: uuid.UUID) -> int:
    return (await db.execute(select(func.count(Run.id)).where(Run.owner_id == user_id))).scalar_one()


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(_: AdminPrincipal, db: DbDep) -> list[AdminUserOut]:
    runs = select(Run.owner_id, func.count(Run.id).label("n")).group_by(Run.owner_id).subquery()
    rows = await db.execute(
        select(User, func.coalesce(runs.c.n, 0))
        .outerjoin(runs, runs.c.owner_id == User.id)
        .order_by(User.created_at)
    )
    return [_out(user, n) for user, n in rows.all()]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID, data: AdminUserPatch, request: Request, principal: AdminWriter, db: DbDep
) -> AdminUserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    demoting = data.role is not None and data.role != "admin" and user.role == "admin"
    deactivating = data.is_active is False and user.is_active
    if user.id == principal.id and (demoting or deactivating):
        raise Conflict("You cannot demote or deactivate your own account")
    if user.role == "admin" and user.is_active and (demoting or deactivating):
        others = select(func.count(User.id)).where(
            User.role == "admin", User.is_active.is_(True), User.id != user.id
        )
        other_admins = (await db.execute(others)).scalar_one()
        if other_admins == 0:
            raise Conflict("Cannot demote or deactivate the last active admin")
    changes: dict[str, object] = {}
    if data.role is not None and data.role != user.role:
        changes["role"] = {"from": user.role, "to": data.role}
        user.role = data.role
    if data.is_active is not None and data.is_active != user.is_active:
        changes["is_active"] = {"from": user.is_active, "to": data.is_active}
        user.is_active = data.is_active
    if data.reset_mfa and (user.totp_enabled or user.totp_secret_enc):
        user.totp_enabled, user.totp_secret_enc, user.totp_recovery = False, None, []
        changes["mfa"] = {"from": "enabled", "to": "reset"}
    if demoting or deactivating:
        # stdio MCP servers run commands on the host; only active admins may keep them.
        disabled = await disable_stdio_servers(db, user.id)
        if disabled:
            changes["stdio_mcp_servers_disabled"] = disabled
    request.state.audit_detail = {"email": user.email, "changes": changes}
    await db.flush()
    return _out(user, await _run_count(db, user.id))
