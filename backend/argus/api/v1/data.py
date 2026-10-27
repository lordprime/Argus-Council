"""Data & privacy: account export / erasure, and admin data-retention controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from argus.api.deps import AdminPrincipal, AdminWriter, CacheDep, DbDep, SessionPrincipal, SettingsDep
from argus.core.errors import ArgusError, Conflict, RateLimited
from argus.core.security import verify_password
from argus.services.account_export import (
    active_runs,
    build_export,
    delete_account,
    file_size,
    iter_file,
    other_active_admins,
)
from argus.services.retention import RetentionService, blob_root

router = APIRouter(tags=["data"])


class InvalidPassword(ArgusError):
    code = "invalid_password"


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class RetentionRunIn(BaseModel):
    dry_run: bool = False


class RetentionOut(BaseModel):
    policy: dict[str, Any]
    last_run: dict[str, Any] | None
    enabled: bool


def _service(request: Request) -> RetentionService:
    svc = getattr(request.app.state, "retention", None)
    if svc is None:
        svc = RetentionService(request.app.state.db.sessionmaker, request.app.state.settings)
        request.app.state.retention = svc
    return svc


async def _throttle(cache: CacheDep, key: str, limit: int) -> None:
    if await cache.hit_rate_limit(key, limit):
        raise RateLimited("Too many attempts, slow down")


# ---------------------------------------------------------------- account
@router.post("/me/export", response_class=StreamingResponse)
async def export_me(
    request: Request, principal: SessionPrincipal, db: DbDep, settings: SettingsDep, cache: CacheDep
) -> StreamingResponse:
    """Download a zip of everything the signed-in user owns (secrets redacted)."""
    await _throttle(cache, f"export:{principal.id}", 5)
    f, manifest = await build_export(db, principal.user, blob_root(settings), limit=settings.export_max_bytes)
    size = file_size(f)
    request.state.audit_detail = {"bytes": size, "counts": manifest["counts"]}
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        iter_file(f),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="argus-export-{stamp}.zip"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    data: DeleteAccountIn,
    request: Request,
    principal: SessionPrincipal,
    db: DbDep,
    settings: SettingsDep,
    cache: CacheDep,
) -> Response:
    """Permanently delete the signed-in account, everything it owns and its artifact blobs."""
    user = principal.user
    await _throttle(cache, f"delete-account:{user.id}", 5)
    if not verify_password(data.password, user.password_hash):
        raise InvalidPassword("Current password is incorrect")
    if user.is_admin and user.is_active and await other_active_admins(db, user.id) == 0:
        raise Conflict("You are the last active admin: promote another admin before deleting your account")
    manager = getattr(request.app.state, "runs", None)
    if manager is not None:
        live = await active_runs(db, user.id)
        for rid in live:
            await manager.cancel(rid)
        for rid in live:
            try:
                await manager.wait(rid, 10)
            except Exception:  # noqa: S112 - best effort; deletion proceeds regardless
                continue
    stats = await delete_account(db, user, blob_root(settings))
    # The audit row must not reference the deleted user (FK); record the id in the detail instead.
    request.state.actor_id = None
    request.state.audit_detail = {"deleted_user_id": str(user.id), **stats}
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- retention (admin)
@router.get("/admin/retention", response_model=RetentionOut)
async def get_retention(request: Request, _: AdminPrincipal, settings: SettingsDep) -> RetentionOut:
    svc = _service(request)
    return RetentionOut(
        policy=svc.policy.as_dict(),
        last_run=svc.last.as_dict() if svc.last else None,
        enabled=settings.retention_enabled,
    )


@router.post("/admin/retention/run")
async def run_retention(
    request: Request, _: AdminWriter, data: RetentionRunIn | None = None
) -> dict[str, Any]:
    """Apply the policy now. With ``dry_run`` nothing is deleted and the counts are returned."""
    dry_run = bool(data and data.dry_run)
    stats = await _service(request).run(dry_run=dry_run)
    request.state.audit_detail = {
        "dry_run": dry_run,
        **{k: v for k, v in stats.as_dict().items() if k != "error"},
    }
    return stats.as_dict()
