"""FastAPI dependencies: settings, DB session, cache, authenticated principal."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.cache.store import Cache
from argus.core.config import Settings
from argus.core.errors import Forbidden, RateLimited, Unauthorized
from argus.core.security import API_KEY_PREFIX, decode_token, hash_api_key
from argus.db.models import ApiKey, User

_bearer = HTTPBearer(auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.db.session():
        yield session


def get_cache(request: Request) -> Cache:
    return request.app.state.cache


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
CacheDep = Annotated[Cache, Depends(get_cache)]


@dataclass
class Principal:
    user: User
    via: str  # "jwt" | "api_key"
    scopes: set[str] = field(default_factory=lambda: {"*"})

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    def require_scope(self, scope: str) -> None:
        if "*" not in self.scopes and scope not in self.scopes:
            raise Forbidden(f"Missing scope: {scope}")


async def _principal_from_api_key(db: AsyncSession, token: str) -> Principal:
    key = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if key is None or key.revoked:
        raise Unauthorized("Invalid API key")
    if key.expires_at is not None and key.expires_at.replace(tzinfo=key.expires_at.tzinfo or UTC) <= now:
        raise Unauthorized("API key expired")
    user = await db.get(User, key.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Inactive user")
    key.last_used_at = now
    return Principal(user=user, via="api_key", scopes=set(key.scopes or ["*"]))


async def resolve_principal(db: AsyncSession, settings: Settings, token: str) -> Principal:
    if token.startswith(API_KEY_PREFIX):
        return await _principal_from_api_key(db, token)
    payload = decode_token(settings, token, "access")
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise Unauthorized("Invalid token subject") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Inactive user")
    return Principal(user=user, via="jwt")


async def get_principal(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    cache: CacheDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    token = creds.credentials if creds else request.headers.get("X-API-Key")
    if not token:
        raise Unauthorized("Authentication required")
    principal = await resolve_principal(db, settings, token)
    if await cache.hit_rate_limit(f"user:{principal.id}", settings.rate_limit_per_minute):
        raise RateLimited("Too many requests")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def require_admin(principal: CurrentPrincipal) -> Principal:
    if not principal.user.is_admin:
        raise Forbidden("Admin privileges required")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]
