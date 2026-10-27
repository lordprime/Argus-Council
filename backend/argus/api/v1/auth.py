from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, status
from sqlalchemy import func, select

from argus.api.deps import CacheDep, CurrentPrincipal, DbDep, SettingsDep
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
from argus.core.errors import Conflict, Forbidden, NotFound, RateLimited, Unauthorized
from argus.core.security import (
    create_token,
    decode_token,
    generate_api_key,
    hash_password,
    needs_rehash,
    verify_password,
)
from argus.db.models import ApiKey, User

router = APIRouter(prefix="/auth", tags=["auth"])

# Equalise timing between unknown-user and wrong-password paths.
_DUMMY_HASH = hash_password("argus-timing-equaliser-password")


def _tokens(settings, user: User) -> TokenOut:
    return TokenOut(
        access_token=create_token(settings, str(user.id), "access"),
        refresh_token=create_token(settings, str(user.id), "refresh"),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def _throttle(cache, request: Request, bucket: str, limit: int = 10) -> None:
    ip = request.client.host if request.client else "unknown"
    if await cache.hit_rate_limit(f"{bucket}:{ip}", limit):
        raise RateLimited("Too many attempts, slow down")


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
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
        is_admin=user_count == 0,
    )
    db.add(user)
    await db.flush()
    return user


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, request: Request, db: DbDep, settings: SettingsDep, cache: CacheDep):
    await _throttle(cache, request, "login")
    user = (await db.execute(select(User).where(User.email == data.email.lower()))).scalar_one_or_none()
    if user is None:
        verify_password(data.password, _DUMMY_HASH)
        raise Unauthorized("Invalid credentials")
    if not verify_password(data.password, user.password_hash) or not user.is_active:
        raise Unauthorized("Invalid credentials")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    return _tokens(settings, user)


@router.post("/refresh", response_model=TokenOut)
async def refresh(data: RefreshIn, db: DbDep, settings: SettingsDep, cache: CacheDep):
    payload = decode_token(settings, data.refresh_token, "refresh")
    revoked_key = cache.key("revoked-jti", payload["jti"])
    if await cache.get(revoked_key):
        raise Unauthorized("Refresh token already used")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("Inactive user")
    # Rotate: each refresh token is single-use.
    remaining = max(int(payload["exp"] - datetime.now(UTC).timestamp()), 1)
    await cache.set(revoked_key, True, ttl=remaining)
    return _tokens(settings, user)


@router.get("/me", response_model=UserOut)
async def me(principal: CurrentPrincipal):
    return principal.user


@router.patch("/me", response_model=UserOut)
async def update_me(data: ProfilePatch, request: Request, principal: CurrentPrincipal, cache: CacheDep):
    user = principal.user
    if data.display_name is not None:
        user.display_name = data.display_name.strip()
    if data.new_password is not None:
        if principal.via == "api_key":
            raise Forbidden("Passwords cannot be changed with an API key")
        await _throttle(cache, request, "password")
        if not verify_password(data.current_password or "", user.password_hash):
            raise Unauthorized("Current password is incorrect")
        user.password_hash = hash_password(data.new_password)
    return user


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_keys(principal: CurrentPrincipal, db: DbDep):
    rows = await db.execute(select(ApiKey).where(ApiKey.user_id == principal.id).order_by(ApiKey.created_at))
    return list(rows.scalars())


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(data: ApiKeyIn, principal: CurrentPrincipal, db: DbDep):
    if principal.via == "api_key":
        raise Forbidden("API keys cannot mint new API keys")
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
async def revoke_key(key_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != principal.id:
        raise NotFound("API key not found")
    key.revoked = True
