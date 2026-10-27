"""Provider connection health: error classification and masked key hints."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx

from argus.db.base import utcnow
from argus.providers.base import ProviderError

HealthStatus = Literal["unknown", "ok", "unreachable", "auth_failed", "rate_limited", "error"]
HEALTH_STATUSES: tuple[HealthStatus, ...] = (
    "unknown",
    "ok",
    "unreachable",
    "auth_failed",
    "rate_limited",
    "error",
)
DETAIL_MAX = 300


def key_hint(api_key: str | None) -> str | None:
    """Display-only fragment of a secret: ``sk-…abcd`` (short keys: ``…cd``)."""
    if not api_key:
        return None
    key = api_key.strip()
    if len(key) < 12:
        return "…" + key[-2:]
    return key[:3] + "…" + key[-4:]


@dataclass
class HealthResult:
    status: HealthStatus
    detail: str | None = None
    latency_ms: int | None = None
    checked_at: datetime = field(default_factory=utcnow)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _detail(text: str) -> str:
    return text.strip()[:DETAIL_MAX]


def classify_error(exc: BaseException) -> tuple[HealthStatus, str]:
    """Map a provider failure to a health status plus a short, key-free detail."""
    if isinstance(exc, ProviderError):
        status = exc.status
        if status in (401, 403):
            return "auth_failed", _detail(str(exc))
        if status == 429:
            return "rate_limited", _detail(str(exc))
        # Status-less retryable errors are transport failures (connect / timeout).
        if status is None and exc.retryable:
            return "unreachable", _detail(str(exc))
        return "error", _detail(str(exc))
    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError, OSError)):
        return "unreachable", _detail(f"{type(exc).__name__}: {exc}")
    return "error", _detail(f"{type(exc).__name__}: {exc}")


def failure(exc: BaseException, latency_ms: int | None = None) -> HealthResult:
    status, detail = classify_error(exc)
    return HealthResult(status=status, detail=detail, latency_ms=latency_ms)
