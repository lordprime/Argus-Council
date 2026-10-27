"""Failure taxonomy for CLI agents and what triggers a seat's fallback chain."""

from __future__ import annotations

import re
from typing import Literal

from argus.providers.base import ProviderError

Category = Literal[
    "auth",  # not logged in, bad key, org not allowed
    "quota",  # subscription usage limit, billing, account on hold
    "rate_limit",  # 429 / overloaded
    "unavailable",  # CLI missing, runner down, timeout, 5xx, network
    "invalid",  # bad request or unknown model
    "session",  # unknown or foreign session id
    "disabled",  # CLI agents switched off on this instance
    "error",
]

# A seat falls back to its next route on these (the policy the owner configures per seat).
FALLBACK_CATEGORIES: frozenset[str] = frozenset({"auth", "quota", "rate_limit", "unavailable"})
_STATUS = {"auth": 401, "quota": 429, "rate_limit": 429, "unavailable": 503, "invalid": 400, "session": 404}


class CLIError(ProviderError):
    def __init__(self, message: str, *, provider: str, category: Category = "error") -> None:
        super().__init__(
            message[:500],
            provider=provider,
            status=_STATUS.get(category),
            # Never retried in place: a CLI turn is slow and a usage limit does not clear in seconds.
            retryable=False,
        )
        self.category: Category = category

    @property
    def fallback(self) -> bool:
        """True when the seat should move on to its fallback route instead of failing."""
        return self.category in FALLBACK_CATEGORIES


# Claude Code's documented error categories (assistant ``error`` / ``system/api_retry``).
CLAUDE_CATEGORIES: dict[str, Category] = {
    "authentication_failed": "auth",
    "oauth_org_not_allowed": "auth",
    "account_on_hold": "quota",
    "billing_error": "quota",
    "rate_limit": "rate_limit",
    "overloaded": "rate_limit",
    "server_error": "unavailable",
    "invalid_request": "invalid",
    "model_not_found": "invalid",
    "max_output_tokens": "error",
    "cloud_credential_error": "auth",
}

# Runner-side failures (``argus.exit.error``).
RUNNER_CATEGORIES: dict[str, Category] = {
    "unknown_session": "session",
    "busy": "unavailable",
    "stream_ended": "unavailable",
    "not_installed": "unavailable",
    "timeout": "unavailable",
    "output_too_large": "error",
    "invalid_user": "invalid",
}

_PATTERNS: list[tuple[re.Pattern[str], Category]] = [
    (
        re.compile(r"usage limit|quota|resource[_ ]exhausted|billing|insufficient[_ ]quota|credit", re.I),
        "quota",
    ),
    (re.compile(r"\b429\b|rate.?limit|too many requests|overloaded|\b529\b", re.I), "rate_limit"),
    (
        re.compile(
            r"\b401\b|\b403\b|unauthori[sz]ed|not logged in|invalid[_ ]api[_ ]key|authenticat|"
            r"login required|please (?:log|sign) ?in|credentials",
            re.I,
        ),
        "auth",
    ),
    (re.compile(r"model.*(?:not found|does not exist|not supported)|invalid model", re.I), "invalid"),
    (
        re.compile(
            r"\b5\d\d\b|timed? ?out|unavailable|connection|network|dns|stream disconnected|econn", re.I
        ),
        "unavailable",
    ),
]


def classify_text(text: str) -> Category:
    for pattern, category in _PATTERNS:
        if pattern.search(text or ""):
            return category
    return "error"


def classify_status(status: int | None) -> Category | None:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status is not None and status >= 500:
        return "unavailable"
    if status in (400, 404):
        return "invalid"
    return None
