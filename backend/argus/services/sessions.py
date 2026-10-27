"""Server-side session revocation for stateless JWTs.

Every login starts a *session* identified by a ``sid`` claim that its access and refresh tokens
share and that survives refresh rotation. Revocation state lives in the cache (Redis in
production) with a TTL equal to the refresh-token lifetime, after which every affected token has
expired on its own:

* ``revoked-sid:<sid>``          - one session ended (logout, refresh-token reuse detected).
* ``sessions-cutoff:<user id>``  - every token of the user issued before ``before`` is void, except
  the session in ``keep`` (password change keeps the session that made the change).
"""

from __future__ import annotations

import time
from typing import Any

from argus.cache.store import Cache
from argus.core.config import Settings


def _ttl(settings: Settings) -> int:
    return settings.refresh_token_ttl_days * 86_400 + 60


async def revoke_session(cache: Cache, settings: Settings, sid: str | None) -> None:
    if sid:
        await cache.set(cache.key("revoked-sid", sid), True, ttl=_ttl(settings))


async def revoke_user_sessions(
    cache: Cache, settings: Settings, user_id: Any, *, keep_sid: str | None = None
) -> None:
    """Void every token of ``user_id`` issued before now, except those of session ``keep_sid``."""
    await cache.set(
        cache.key("sessions-cutoff", str(user_id)),
        {"before": int(time.time()), "keep": keep_sid},
        ttl=_ttl(settings),
    )


async def session_revoked(cache: Cache, payload: dict[str, Any]) -> bool:
    """Whether the access token's session was ended (logout, refresh reuse, password change).

    Checked on every authenticated request, so it reads best-effort: during a Redis outage a
    revoked access token keeps working until it expires (ARGUS_ACCESS_TOKEN_TTL_MINUTES) rather
    than every request failing (M8). Refresh-token single-use stays fail-closed, so a revoked
    session can never be renewed; the degraded read is counted in argus_cache_degraded_total.
    """
    sid = payload.get("sid")
    if sid and await cache.get_best_effort(cache.key("revoked-sid", str(sid))):
        return True
    cutoff = await cache.get_best_effort(cache.key("sessions-cutoff", str(payload.get("sub"))))
    if not cutoff:
        return False
    if sid and sid == cutoff.get("keep"):
        return False
    return int(payload.get("iat", 0)) < int(cutoff.get("before", 0))
