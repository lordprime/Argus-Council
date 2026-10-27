from __future__ import annotations

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, SettingsDep, write_scope
from argus.api.schemas import WebhookIn, WebhookPatch
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, NotFound
from argus.db.models import Webhook, WebhookDelivery
from argus.services.llm import validate_base_url
from argus.services.webhooks import EVENTS

router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[write_scope("webhooks:write")])


def _check(url: str, events: list[str], settings) -> str:  # type: ignore[no-untyped-def]
    unknown = sorted(set(events) - set(EVENTS) - {"*"})
    if unknown:
        raise ArgusError(f"Unknown events: {', '.join(unknown)}", details={"allowed": list(EVENTS)})
    clean = validate_base_url(url, settings.connections_allow_private_urls, require_dns=False)
    if settings.is_production and not clean.startswith("https://"):
        raise ArgusError("Webhook URLs must use https in production")
    return clean


def _out(h: Webhook) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "url": h.url,
        "events": h.events,
        "description": h.description,
        "enabled": h.enabled,
        "failure_count": h.failure_count,
        "last_status": h.last_status,
        "last_delivery_at": h.last_delivery_at.isoformat() if h.last_delivery_at else None,
        "created_at": h.created_at.isoformat(),
    }


async def _own(db, owner_id: uuid.UUID, hook_id: uuid.UUID) -> Webhook:
    hook = await db.get(Webhook, hook_id)
    if hook is None or hook.owner_id != owner_id:
        raise NotFound("Webhook not found")
    return hook


@router.get("/events")
async def list_events(_: CurrentPrincipal) -> dict:
    return {"events": list(EVENTS)}


@router.get("")
async def list_hooks(principal: CurrentPrincipal, db: DbDep) -> dict:
    rows = await db.execute(
        select(Webhook).where(Webhook.owner_id == principal.id).order_by(Webhook.created_at)
    )
    return {"webhooks": [_out(h) for h in rows.scalars()]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_hook(data: WebhookIn, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep) -> dict:
    url = _check(data.url, data.events, settings)
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    hook = Webhook(
        owner_id=principal.id,
        url=url,
        events=data.events,
        description=data.description,
        enabled=data.enabled,
        secret_enc=SecretBox(settings).encrypt(secret),
    )
    db.add(hook)
    await db.flush()
    return {**_out(hook), "secret": secret}


@router.patch("/{hook_id}")
async def update_hook(
    hook_id: uuid.UUID, data: WebhookPatch, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    hook = await _own(db, principal.id, hook_id)
    values = data.model_dump(exclude_unset=True)
    if "url" in values or "events" in values:
        values["url"] = _check(values.get("url") or hook.url, values.get("events") or hook.events, settings)
    if values.get("enabled"):
        hook.failure_count = 0
    for k, v in values.items():
        setattr(hook, k, v)
    await db.flush()
    return _out(hook)


@router.post("/{hook_id}/rotate-secret")
async def rotate(hook_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep) -> dict:
    hook = await _own(db, principal.id, hook_id)
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    hook.secret_enc = SecretBox(settings).encrypt(secret)
    return {**_out(hook), "secret": secret}


@router.delete("/{hook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hook(hook_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    await db.delete(await _own(db, principal.id, hook_id))


@router.post("/{hook_id}/test")
async def test_hook(hook_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    hook = await _own(db, principal.id, hook_id)
    ok = await request.app.state.webhooks.deliver(hook, "ping", {"message": "Hello from Argus Council"})
    await db.refresh(hook)
    return {"ok": ok, **_out(hook)}


@router.get("/{hook_id}/deliveries")
async def deliveries(
    hook_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, limit: int = Query(default=50, ge=1, le=200)
) -> dict:
    await _own(db, principal.id, hook_id)
    rows = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == hook_id)
        .order_by(WebhookDelivery.id.desc())
        .limit(limit)
    )
    return {
        "deliveries": [
            {
                "id": d.id,
                "ts": d.ts.isoformat(),
                "event": d.event,
                "delivery_id": d.delivery_id,
                "attempt": d.attempt,
                "status_code": d.status_code,
                "ok": d.ok,
                "error": d.error,
                "duration_ms": d.duration_ms,
            }
            for d in rows.scalars()
        ]
    }
