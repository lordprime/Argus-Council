"""Password hashing, JWT issuance/verification and API key helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from argus.core.config import Settings
from argus.core.errors import Unauthorized

_hasher = PasswordHasher()
_ALGORITHM = "HS256"
API_KEY_PREFIX = "argus_"

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def create_token(settings: Settings, subject: str, token_type: TokenType, **claims: Any) -> str:
    now = datetime.now(UTC)
    ttl = (
        timedelta(minutes=settings.access_token_ttl_minutes)
        if token_type == "access"  # noqa: S105
        else timedelta(days=settings.refresh_token_ttl_days)
    )
    payload = {
        "sub": subject,
        "typ": token_type,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "iss": "argus-council",
        **claims,
    }
    return jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=_ALGORITHM)


def decode_token(settings: Settings, token: str, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[_ALGORITHM],
            issuer="argus-council",
            options={"require": ["exp", "iat", "sub", "typ", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid or expired token") from exc
    if payload.get("typ") != expected_type:
        raise Unauthorized("Wrong token type")
    return payload


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, sha256_hash)``. Only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{API_KEY_PREFIX}{raw}"
    return plaintext, plaintext[: len(API_KEY_PREFIX) + 8], hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
