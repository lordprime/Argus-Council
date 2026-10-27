"""Time-based one-time passwords (RFC 6238, the authenticator-app standard) and recovery codes.

HMAC-SHA1, 30-second steps, 6 digits: what Google Authenticator, 1Password, Aegis and others use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP_S = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next step too (clock drift)
RECOVERY_CODES = 10


def generate_secret() -> str:
    """A 160-bit random secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def counter_at(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_S)


def match(secret: str, code: str, now: float | None = None) -> int | None:
    """Return the time step ``code`` is valid for (within the drift window), or ``None``.

    The caller records the step so one code cannot be used twice.
    """
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = counter_at(now)
    found = None
    for counter in range(current - WINDOW, current + WINDOW + 1):
        # Compare every candidate so timing does not reveal which step matched.
        if hmac.compare_digest(code_at(secret, counter), code):
            found = counter
    return found


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP_S}
    )
    return f"otpauth://totp/{label}?{query}"


def new_recovery_codes(n: int = RECOVERY_CODES) -> list[str]:
    """Single-use codes like ``k7f3-9q2m-xw4d`` (60 bits each), shown once."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)) for _ in range(n)]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
