"""Admin-only user management (RBAC) and feature switches. Mutations are recorded by the audit middleware."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import func, select

from argus.api.deps import AdminPrincipal, AdminWriter, DbDep
from argus.core.errors import Conflict, NotFound
from argus.db.models import FeatureSwitch, Run, User
from argus.services.connections import disable_stdio_servers
from argus.services.features import (
    FEATURE_KEYS,
    FEATURE_LABELS,
    INSTALL_COMMANDS,
    FeatureKey,
    FeatureStatus,
    FeatureSwitches,
)

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


# ---------------------------------------------------------------------------- feature switches
class FeatureOut(BaseModel):
    key: FeatureKey
    label: str
    installed: bool
    status: FeatureStatus
    # The admin's stored switch (on when never set); ``active`` is what agents actually get.
    enabled: bool
    active: bool
    install_command: str
    updated_at: datetime | None
    updated_by: str | None


class FeaturePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


def _features(request: Request) -> FeatureSwitches:
    return request.app.state.features


async def _feature_outs(db: DbDep, features: FeatureSwitches, keys: tuple[str, ...]) -> list[FeatureOut]:
    rows = {
        row.key: (row, email)
        for row, email in (
            await db.execute(
                select(FeatureSwitch, User.email)
                .outerjoin(User, User.id == FeatureSwitch.updated_by_id)
                .where(FeatureSwitch.key.in_(keys))
            )
        ).all()
    }
    statuses = await asyncio.gather(*(features.probe(key) for key in keys))
    out = []
    for key, status in zip(keys, statuses, strict=True):
        row, email = rows.get(key, (None, None))
        enabled = True if row is None else bool(row.enabled)
        installed = features.installed(key)
        out.append(
            FeatureOut(
                key=key,  # type: ignore[arg-type]
                label=FEATURE_LABELS[key],
                installed=installed,
                status=status,
                enabled=enabled,
                active=installed and enabled,
                install_command=INSTALL_COMMANDS[key],
                updated_at=row.updated_at if row is not None else None,
                updated_by=email,
            )
        )
    return out


@router.get("/features", response_model=list[FeatureOut])
async def list_features(_: AdminPrincipal, db: DbDep, request: Request) -> list[FeatureOut]:
    """The optional features (live browser, CLI agents): installed on the server, reachable, on or off."""
    return await _feature_outs(db, _features(request), FEATURE_KEYS)


@router.patch("/features/{key}", response_model=FeatureOut)
async def update_feature(
    key: str, data: FeaturePatch, request: Request, principal: AdminWriter, db: DbDep
) -> FeatureOut:
    """Turn an installed feature on or off for everyone, from its next use (runs in progress included)."""
    if key not in FEATURE_KEYS:
        raise NotFound("Feature not found")
    features = _features(request)
    if data.enabled and not features.installed(key):
        raise Conflict(f"{FEATURE_LABELS[key]} is not installed on this server: run {INSTALL_COMMANDS[key]}")
    previous = await features.set(key, data.enabled, principal.id, db=db)
    request.state.audit_detail = {"feature": key, "enabled": {"from": previous, "to": data.enabled}}
    return (await _feature_outs(db, features, (key,)))[0]
