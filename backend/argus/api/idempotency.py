"""Idempotency-Key support: safely retry POSTs without duplicating side effects.

The first request with a key runs normally and its response is stored for 24 hours,
scoped to the caller and the path. Replays return the stored response with
``Idempotent-Replayed: true``. A replay while the first request is still running gets 409.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from argus.cache.store import Cache
from argus.core.errors import ArgusError, Conflict

TTL_S = 24 * 3600


async def idempotent(
    request: Request, cache: Cache, owner: str, status_code: int, run: Callable[[], Awaitable[Any]]
) -> Any:
    key = request.headers.get("idempotency-key")
    if not key:
        return await run()
    if len(key) > 100:
        raise ArgusError("Idempotency-Key must be at most 100 characters")
    body = await request.body()
    scope = hashlib.sha256(f"{owner}|{request.method}|{request.url.path}|{key}".encode()).hexdigest()
    store_key = cache.key("idem", scope)
    stored = await cache.get(store_key)
    if stored is not None:
        if stored["body_sha"] != hashlib.sha256(body).hexdigest():
            raise Conflict("Idempotency-Key was already used with a different request body")
        return JSONResponse(
            stored["response"], status_code=stored["status"], headers={"Idempotent-Replayed": "true"}
        )
    if await cache.backend.incr_window(cache.key("idem-lock", scope), 120) > 1:
        raise Conflict("A request with this Idempotency-Key is still in progress")
    try:
        result = await run()
    finally:
        await cache.delete(cache.key("idem-lock", scope))
    await cache.set(
        store_key,
        {
            "status": status_code,
            "response": jsonable_encoder(result),
            "body_sha": hashlib.sha256(body).hexdigest(),
        },
        ttl=TTL_S,
    )
    return result
