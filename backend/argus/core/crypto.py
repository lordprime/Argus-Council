"""Symmetric encryption for secrets at rest (provider API keys, MCP tokens)."""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from argus.core.config import Settings


class SecretBox:
    def __init__(self, settings: Settings) -> None:
        material = (settings.encryption_key or settings.secret_key).get_secret_value().encode()
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"argus-council/v1", info=b"secret-box").derive(
            material
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Secret could not be decrypted (was the secret key rotated?)") from exc


def mask(secret: str | None) -> str | None:
    if not secret:
        return None
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 12 else "…"
