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
# Scopes an API key can hold. Reads need no scope; each router's state-changing routes need its scope.
WRITE_SCOPES = (
    "runs:write",
    "councils:write",
    "providers:write",
    "skills:write",
    "connections:write",
    "webhooks:write",
    "knowledge:write",
)
API_KEY_PREFIX = "argus_"
API_KEY_SALT = b"argus-council/api-key"
API_KEY_ROUNDS = 10_000
# ``ApiKey.hash_scheme`` values: keys created before 0.1.2 are "sha256" until first used.
API_KEY_SCHEME = "pbkdf2"
LEGACY_API_KEY_SCHEME = "sha256"

# "mfa": a short-lived proof that the password was right, exchanged for real tokens at
# POST /auth/mfa/verify together with a second-factor code. It authenticates nothing else.
TokenType = Literal["access", "refresh", "mfa"]
MFA_TOKEN_TTL = timedelta(minutes=5)


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
    ttl = {
        "access": timedelta(minutes=settings.access_token_ttl_minutes),
        "refresh": timedelta(days=settings.refresh_token_ttl_days),
        "mfa": MFA_TOKEN_TTL,
    }[token_type]
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
    """Return ``(plaintext, prefix, key_hash)``. Only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"{API_KEY_PREFIX}{raw}"
    return plaintext, plaintext[: len(API_KEY_PREFIX) + 8], hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    """PBKDF2-SHA256 with a fixed salt, so a key is still found by an indexed lookup on its hash.

    A key carries 256 random bits, so guessing it is hopeless however fast the hash is; the
    modest work factor keeps every authenticated request cheap. 32 bytes, 64 hex characters."""
    return hashlib.pbkdf2_hmac("sha256", plaintext.encode(), API_KEY_SALT, API_KEY_ROUNDS).hex()


def legacy_hash_api_key(plaintext: str) -> str:
    """How keys were hashed before 0.1.2 (plain SHA-256). Used only to recognise such a key once,
    on its first use, and re-hash it with :func:`hash_api_key`."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
