"""Audit trail for security-relevant actions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from argus.core.logging import get_logger
from argus.db.models import AuditEvent

log = get_logger(__name__)
_SENSITIVE = ("password", "api_key", "token", "secret", "headers", "key")


def _scrub(detail: dict[str, Any]) -> dict[str, Any]:
    return {k: "[redacted]" if any(s in k.lower() for s in _SENSITIVE) else v for k, v in detail.items()}


async def audit(
    request: Request,
    action: str,
    *,
    actor_id: uuid.UUID | None,
    target_type: str = "",
    target_id: str | uuid.UUID = "",
    success: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        async with request.app.state.db.sessionmaker() as s:
            s.add(
                AuditEvent(
                    ts=datetime.now(UTC),
                    actor_id=actor_id,
                    action=action,
                    target_type=target_type,
                    target_id=str(target_id)[:120],
                    success=success,
                    ip=(request.client.host if request.client else "")[:64],
                    user_agent=request.headers.get("user-agent", "")[:200],
                    detail=_scrub(detail or {}),
                )
            )
            await s.commit()
    except Exception as exc:
        log.error("audit.write_failed", action=action, error=str(exc)[:200])
