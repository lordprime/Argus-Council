from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func, select

from argus.api.deps import CacheDep, CurrentPrincipal, DbDep, SessionPrincipal, SettingsDep
from argus.api.schemas import (
    ApiKeyCreated,
    ApiKeyIn,
    ApiKeyOut,
    LoginIn,
    ProfilePatch,
    RefreshIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
from argus.core.errors import ArgusError, Conflict, Forbidden, NotFound, RateLimited, Unauthorized
from argus.core.security import (
    create_token,
    decode_token,
    generate_api_key,
    hash_password,
    needs_rehash,
    verify_password,
)
from argus.db.models import ApiKey, User
from argus.services.sessions import revoke_session, revoke_user_sessions, session_revoked

router = APIRouter(prefix="/auth", tags=["auth"])


class AccountOut(UserOut):
    """UserOut plus the RBAC role ("admin" | "member" | "viewer"); `is_admin` mirrors role == "admin"."""

    role: Literal["admin", "member", "viewer"]


class InvalidPassword(ArgusError):
    code = "invalid_password"


# Equalise timing between unknown-user and wrong-password paths.
_DUMMY_HASH = hash_password("argus-timing-equaliser-password")


def _tokens(settings, user: User, sid: str | None = None) -> TokenOut:
    # Access and refresh tokens of one sign-in share a session id, kept across refresh rotation,
    # so logout or refresh-token reuse can end the whole session server-side.
    sid = sid or uuid.uuid4().hex
    return TokenOut(
        access_token=create_token(settings, str(user.id), "access", sid=sid),
        refresh_token=create_token(settings, str(user.id), "refresh", sid=sid),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def _throttle(cache, request: Request, bucket: str, limit: int = 10) -> None:
    ip = request.client.host if request.client else "unknown"
    if await cache.hit_rate_limit(f"{bucket}:{ip}", limit):
        raise RateLimited("Too many attempts, slow down")


@router.post("/register", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterIn, request: Request, db: DbDep, settings: SettingsDep, cache: CacheDep):
    await _throttle(cache, request, "register", 5)
    user_count = (await db.execute(select(func.count(User.id)))).scalar_one()
    if user_count > 0 and not settings.allow_registration:
        raise Forbidden("Registration is disabled")
    email = data.email.lower()
    if (await db.execute(select(User.id).where(User.email == email))).first():
        raise Conflict("Email already registered")
    # The very first account becomes the administrator.
    user = User(
        email=email,
        display_name=data.display_name,
        password_hash=hash_password(data.password),
        role="admin" if user_count == 0 else "member",
    )
    db.add(user)
    await db.flush()
    return user


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, request: Request, db: DbDep, settings: SettingsDep, cache: CacheDep):
    await _throttle(cache, request, "login")
    # Per-account limit as well: client IPs can be spread or spoofed, the target account cannot.
    if await cache.hit_rate_limit(f"login-account:{cache.fingerprint(data.email.lower())[:32]}", 10):
        raise RateLimited("Too many attempts for this account, try again in a minute")
    request.state.audit_detail = {"email": data.email.lower()}
    user = (await db.execute(select(User).where(User.email == data.email.lower()))).scalar_one_or_none()
    if user is None:
        verify_password(data.password, _DUMMY_HASH)
        raise Unauthorized("Invalid credentials")
    if not verify_password(data.password, user.password_hash) or not user.is_active:
        raise Unauthorized("Invalid credentials")
    request.state.actor_id = user.id
    user.last_login_at = datetime.now(UTC)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    return _tokens(settings, user)


@router.post("/refresh", response_model=TokenOut)
async def refresh(data: RefreshIn, db: DbDep, settings: SettingsDep, cache: CacheDep):
    payload = decode_token(settings, data.refresh_token, "refresh")
    if await session_revoked(cache, payload):
        raise Unauthorized("Session has been signed out")
    # Rotate: each refresh token is single-use. The counter makes the check-and-mark atomic, so two
    # concurrent uses of one token cannot both succeed.
    remaining = max(int(payload["exp"] - datetime.now(UTC).timestamp()), 1)
    uses = await cache.incr_window(cache.key("refresh-used", payload["jti"]), remaining)
    if uses > 1:
        # Reuse means the token leaked (or was replayed): end the whole session, both holders.
        await revoke_session(cache, settings, payload.get("sid"))
        raise Unauthorized("Refresh token already used")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("Inactive user")
    return _tokens(settings, user, payload.get("sid"))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshIn, settings: SettingsDep, cache: CacheDep) -> Response:
    """End the session the refresh token belongs to (its access tokens stop working too)."""
    try:
        payload = decode_token(settings, data.refresh_token, "refresh")
    except Unauthorized:
        return Response(status_code=status.HTTP_204_NO_CONTENT)  # already invalid: nothing to end
    await revoke_session(cache, settings, payload.get("sid"))
    remaining = max(int(payload["exp"] - datetime.now(UTC).timestamp()), 1)
    await cache.incr_window(cache.key("refresh-used", payload["jti"]), remaining)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=AccountOut)
async def me(principal: CurrentPrincipal):
    return principal.user


@router.patch("/me", response_model=AccountOut)
async def update_me(
    data: ProfilePatch, request: Request, principal: CurrentPrincipal, cache: CacheDep, settings: SettingsDep
):
    user = principal.user
    if data.new_password is not None and principal.via != "jwt":
        raise Forbidden("API keys cannot change the account password")
    if data.display_name is not None:
        user.display_name = data.display_name.strip()
    if data.new_password is not None:
        if principal.via == "api_key":
            raise Forbidden("Passwords cannot be changed with an API key")
        await _throttle(cache, request, "password")
        if not verify_password(data.current_password or "", user.password_hash):
            # 400, not 401: the session is valid; only the confirmation is wrong.
            raise InvalidPassword("Current password is incorrect")
        user.password_hash = hash_password(data.new_password)
        # Sign out every other session; the one that made the change stays signed in.
        await revoke_user_sessions(cache, settings, user.id, keep_sid=principal.sid)
    return user


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_keys(principal: CurrentPrincipal, db: DbDep):
    rows = await db.execute(select(ApiKey).where(ApiKey.user_id == principal.id).order_by(ApiKey.created_at))
    return list(rows.scalars())


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(data: ApiKeyIn, principal: SessionPrincipal, db: DbDep):
    if principal.is_viewer and set(data.scopes) - {"read"}:
        raise Forbidden('Viewers have read-only access: API keys must use scopes ["read"]')
    plaintext, prefix, digest = generate_api_key()
    key = ApiKey(
        user_id=principal.id,
        name=data.name,
        prefix=prefix,
        key_hash=digest,
        scopes=data.scopes,
        expires_at=datetime.now(UTC) + timedelta(days=data.expires_in_days) if data.expires_in_days else None,
    )
    db.add(key)
    await db.flush()
    return ApiKeyCreated.model_validate({**ApiKeyOut.model_validate(key).model_dump(), "key": plaintext})


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: uuid.UUID, principal: SessionPrincipal, db: DbDep):
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != principal.id:
        raise NotFound("API key not found")
    key.revoked = True
