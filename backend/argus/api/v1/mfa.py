"""Optional TOTP two-factor authentication.

Enrolment: ``POST /auth/mfa/setup`` (password) returns a secret and an ``otpauth://`` URI for an
authenticator app; ``POST /auth/mfa/enable`` (a current code) switches it on and returns ten
single-use recovery codes, shown once.

Sign-in: with MFA on, ``POST /auth/login`` answers ``{"mfa_required": true, "mfa_token": ...}``
instead of tokens. The ``mfa_token`` (5 minutes, at most 5 attempts) plus a code from the app, or
a recovery code, is exchanged for tokens at ``POST /auth/mfa/verify``.

Each authenticator code works once (its time step is recorded), recovery codes are consumed, and
every check is rate limited per account and per client IP. API keys are unaffected (creating one
already needs a signed-in session, which now needs the second factor).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from argus.api.deps import CacheDep, DbDep, SessionPrincipal, SettingsDep
from argus.api.schemas import TokenOut
from argus.cache.store import Cache
from argus.core import totp
from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, Conflict, RateLimited, Unauthorized
from argus.core.security import MFA_TOKEN_TTL, create_token, decode_token, verify_password
from argus.db.models import User
from argus.services.sessions import revoke_user_sessions

router = APIRouter(prefix="/auth/mfa", tags=["auth"])

MAX_ATTEMPTS_PER_CHALLENGE = 5
ISSUER = "Argus Council"


class InvalidCode(ArgusError):
    code = "invalid_mfa_code"


class InvalidPassword(ArgusError):
    code = "invalid_password"


class MfaChallengeOut(BaseModel):
    mfa_required: Literal[True] = True
    mfa_token: str
    expires_in: int


class MfaStatusOut(BaseModel):
    enabled: bool
    pending: bool
    recovery_codes_left: int


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=32, description="Authenticator code or recovery code")


class PasswordCodeIn(PasswordIn, CodeIn):
    pass


class VerifyIn(CodeIn):
    mfa_token: str = Field(min_length=1, max_length=4096)


class SetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


# ------------------------------------------------------------------ helpers
def challenge(settings: Settings, user: User) -> MfaChallengeOut:
    return MfaChallengeOut(
        mfa_token=create_token(settings, str(user.id), "mfa"),
        expires_in=int(MFA_TOKEN_TTL.total_seconds()),
    )


async def _limit(cache: Cache, request: Request, user_id: uuid.UUID) -> None:
    ip = request.client.host if request.client else "unknown"
    if await cache.hit_rate_limit(f"mfa-user:{user_id}", 5) or await cache.hit_rate_limit(f"mfa-ip:{ip}", 20):
        raise RateLimited("Too many attempts, try again in a minute")


async def check_second_factor(
    cache: Cache, settings: Settings, user: User, code: str, *, allow_recovery: bool = True
) -> str:
    """Accept a fresh authenticator code or (consumes it) a recovery code; returns which one."""
    if not user.totp_secret_enc:
        raise InvalidCode("Two-factor authentication is not set up")
    secret = SecretBox(settings).decrypt(user.totp_secret_enc)
    step = totp.match(secret, code)
    if step is not None:
        # One use per time step: a code read over someone's shoulder cannot be replayed.
        uses = await cache.incr_window(cache.key("totp-used", str(user.id), str(step)), 3 * totp.STEP_S)
        if uses == 1:
            return "totp"
        raise InvalidCode("That code was already used; wait for the next one")
    if allow_recovery and user.totp_enabled:
        digest = totp.hash_recovery_code(code)
        remaining = [h for h in (user.totp_recovery or []) if h != digest]
        if len(remaining) < len(user.totp_recovery or []):
            user.totp_recovery = remaining
            return "recovery"
    raise InvalidCode("Invalid authentication code")


def _new_recovery_codes(user: User) -> list[str]:
    codes = totp.new_recovery_codes()
    user.totp_recovery = [totp.hash_recovery_code(c) for c in codes]
    return codes


def _require_password(user: User, password: str) -> None:
    if not verify_password(password, user.password_hash):
        raise InvalidPassword("Current password is incorrect")


# ------------------------------------------------------------------ enrolment
@router.get("", response_model=MfaStatusOut)
async def status(principal: SessionPrincipal) -> MfaStatusOut:
    u = principal.user
    return MfaStatusOut(
        enabled=bool(u.totp_enabled),
        pending=bool(u.totp_secret_enc) and not u.totp_enabled,
        recovery_codes_left=len(u.totp_recovery or []) if u.totp_enabled else 0,
    )


@router.post("/setup", response_model=SetupOut)
async def setup(
    data: PasswordIn, request: Request, principal: SessionPrincipal, settings: SettingsDep, cache: CacheDep
) -> SetupOut:
    """Start enrolment (needs the password, so a borrowed session cannot lock the owner out)."""
    user = principal.user
    await _limit(cache, request, user.id)
    if user.totp_enabled:
        raise Conflict("Two-factor authentication is already on; turn it off first")
    _require_password(user, data.password)
    secret = totp.generate_secret()
    user.totp_secret_enc = SecretBox(settings).encrypt(secret)
    request.state.audit_detail = {"mfa": "setup"}
    return SetupOut(secret=secret, otpauth_uri=totp.provisioning_uri(secret, user.email, ISSUER))


@router.post("/enable", response_model=RecoveryCodesOut)
async def enable(
    data: CodeIn, request: Request, principal: SessionPrincipal, settings: SettingsDep, cache: CacheDep
) -> RecoveryCodesOut:
    user = principal.user
    await _limit(cache, request, user.id)
    if user.totp_enabled:
        raise Conflict("Two-factor authentication is already on")
    if not user.totp_secret_enc:
        raise Conflict("Start with /auth/mfa/setup")
    await check_second_factor(cache, settings, user, data.code, allow_recovery=False)
    user.totp_enabled = True
    codes = _new_recovery_codes(user)
    # Sessions opened with the password alone end; this one (which just proved the code) stays.
    await revoke_user_sessions(cache, settings, user.id, keep_sid=principal.sid)
    request.state.audit_detail = {"mfa": "enabled"}
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/disable", response_model=MfaStatusOut)
async def disable(
    data: PasswordCodeIn,
    request: Request,
    principal: SessionPrincipal,
    settings: SettingsDep,
    cache: CacheDep,
) -> MfaStatusOut:
    user = principal.user
    await _limit(cache, request, user.id)
    _require_password(user, data.password)
    if user.totp_enabled:
        await check_second_factor(cache, settings, user, data.code)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_recovery = []
    request.state.audit_detail = {"mfa": "disabled"}
    return MfaStatusOut(enabled=False, pending=False, recovery_codes_left=0)


@router.post("/recovery-codes", response_model=RecoveryCodesOut)
async def regenerate(
    data: PasswordCodeIn,
    request: Request,
    principal: SessionPrincipal,
    settings: SettingsDep,
    cache: CacheDep,
) -> RecoveryCodesOut:
    user = principal.user
    await _limit(cache, request, user.id)
    if not user.totp_enabled:
        raise Conflict("Two-factor authentication is off")
    _require_password(user, data.password)
    await check_second_factor(cache, settings, user, data.code, allow_recovery=False)
    request.state.audit_detail = {"mfa": "recovery_codes_regenerated"}
    return RecoveryCodesOut(recovery_codes=_new_recovery_codes(user))


# ------------------------------------------------------------------ sign-in step
@router.post("/verify", response_model=TokenOut)
async def verify(
    data: VerifyIn, request: Request, db: DbDep, settings: SettingsDep, cache: CacheDep
) -> TokenOut:
    """Second step of sign-in: the challenge token from /auth/login plus a code."""
    from argus.api.v1.auth import issue_tokens

    payload = decode_token(settings, data.mfa_token, "mfa")
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise Unauthorized("Invalid token subject") from exc
    await _limit(cache, request, user_id)
    remaining = max(int(payload["exp"] - datetime.now(UTC).timestamp()), 1)
    if await cache.get(cache.key("mfa-spent", payload["jti"])):
        raise Unauthorized("This sign-in was already completed; sign in again")
    attempts = await cache.incr_window(cache.key("mfa-challenge", payload["jti"]), remaining)
    if attempts > MAX_ATTEMPTS_PER_CHALLENGE:
        raise Unauthorized("Too many wrong codes; sign in again")
    user = await db.get(User, user_id)
    if user is None or not user.is_active or not user.totp_enabled:
        raise Unauthorized("Sign in again")
    request.state.actor_id = user.id
    method = await check_second_factor(cache, settings, user, data.code)
    # The challenge is spent: it cannot mint a second session.
    await cache.set(cache.key("mfa-spent", payload["jti"]), True, ttl=remaining)
    user.last_login_at = datetime.now(UTC)
    request.state.audit_detail = {"mfa": method}
    return issue_tokens(settings, user)
