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
from argus.services.sessions import session_revoked

_bearer = HTTPBearer(auto_error=False)

VIEWER_READ_ONLY = "Viewers have read-only access"


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.db.session():
        yield session


def get_cache(request: Request) -> Cache:
    return request.app.state.cache


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
# scope="function": commit before the response is sent, so clients never observe a 2xx for an
# uncommitted write (e.g. create-then-immediately-delete).
DbDep = Annotated[AsyncSession, Depends(get_db, scope="function")]
CacheDep = Annotated[Cache, Depends(get_cache)]


@dataclass
class Principal:
    user: User
    via: str  # "jwt" | "api_key"
    scopes: set[str] = field(default_factory=lambda: {"*"})
    sid: str | None = None  # session id of a JWT principal (see argus.services.sessions)

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> str:
        return self.user.role or "member"

    @property
    def is_viewer(self) -> bool:
        return self.role == "viewer"

    def require_writer(self) -> None:
        """Viewers are read-only everywhere, whatever their API key's scopes say."""
        if self.is_viewer:
            raise Forbidden(VIEWER_READ_ONLY)

    def require_scope(self, scope: str) -> None:
        if scope != "read":
            self.require_writer()
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
    # An empty scope list means "no write scopes" (read-only), never full access.
    scopes = {"*"} if key.scopes is None else set(key.scopes)
    return Principal(user=user, via="api_key", scopes=scopes)


async def resolve_principal(
    db: AsyncSession, settings: Settings, token: str, cache: Cache | None = None
) -> Principal:
    if token.startswith(API_KEY_PREFIX):
        return await _principal_from_api_key(db, token)
    payload = decode_token(settings, token, "access")
    if cache is not None and await session_revoked(cache, payload):
        raise Unauthorized("Session has been signed out")
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise Unauthorized("Invalid token subject") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("Inactive user")
    return Principal(user=user, via="jwt", sid=payload.get("sid"))


async def get_principal(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    cache: CacheDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    # Memoised so the router-level scope check and the endpoint share one lookup and one rate-limit hit.
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = creds.credentials if creds else request.headers.get("X-API-Key")
    if not token:
        raise Unauthorized("Authentication required")
    principal = await resolve_principal(db, settings, token, cache)
    request.state.actor_id = principal.id
    if await cache.hit_rate_limit(f"user:{principal.id}", settings.rate_limit_per_minute):
        raise RateLimited("Too many requests")
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def write_scope(scope: str, *, exempt: tuple[str, ...] = ()) -> object:
    """Router dependency: every state-changing method on the router needs `scope`.

    Reads are left to the endpoint's own auth (some, like the SSE stream, use a ticket instead).
    `exempt` lists route-path suffixes for POSTs that change nothing, such as validation.
    Viewers are rejected here with 403 regardless of scope.
    """

    async def check(
        request: Request,
        db: DbDep,
        settings: SettingsDep,
        cache: CacheDep,
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if request.method in _SAFE_METHODS:
            return
        if exempt and getattr(request.scope.get("route"), "path", "").endswith(exempt):
            return
        principal = await get_principal(request, db, settings, cache, creds)
        principal.require_writer()
        principal.require_scope(scope)

    return Depends(check)


async def require_session(principal: CurrentPrincipal) -> Principal:
    """Account management (keys, password) needs an interactive sign-in, never an API key."""
    if principal.via != "jwt":
        raise Forbidden("This action requires signing in; API keys cannot manage the account")
    return principal


SessionPrincipal = Annotated[Principal, Depends(require_session)]


async def require_admin(principal: CurrentPrincipal) -> Principal:
    if not principal.user.is_admin:
        raise Forbidden("Admin privileges required")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


async def require_admin_writer(principal: AdminPrincipal) -> Principal:
    """Admin mutations: an interactive session, or an API key holding the full ``*`` scope.

    A narrowly scoped (e.g. read-only) key belonging to an admin must not change other accounts.
    """
    if principal.via != "jwt" and "*" not in principal.scopes:
        raise Forbidden("Admin changes need a signed-in session or an API key with the '*' scope")
    return principal


AdminWriter = Annotated[Principal, Depends(require_admin_writer)]
