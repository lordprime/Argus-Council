"""Liveness and readiness.

* ``/health/live`` is pure liveness (the Docker HEALTHCHECK): the process serves HTTP. It never
  touches a dependency, so a Redis or Postgres outage does not get the container restarted.
* ``/health/ready`` answers "can this instance do its job": 200 when Postgres and Redis both answer,
  503 otherwise (deploy gating in ``scripts/lib.sh:wait_healthy`` and the ``ArgusNotReady`` alert).
  Redis counts as required because refresh tokens, stream tickets and idempotency fail closed
  without it, even though most requests keep working (see ``argus/cache/store.py``).
  Each check is bounded by ``ARGUS_HEALTH_CHECK_TIMEOUT_S`` so a hung dependency cannot stall
  the probe. ``optional`` reports dependencies whose loss only disables a feature (the code
  sandbox: ``run_command`` fails) and never changes the status code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import httpx
from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from argus import __version__

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict:
    return {"status": "ok", "version": __version__}


async def _bounded(check: Awaitable[bool], timeout_s: float) -> bool:
    try:
        return bool(await asyncio.wait_for(check, timeout_s))
    except Exception:
        return False


async def _database(request: Request) -> bool:
    async with request.app.state.db.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def _sandbox(url: str, timeout_s: float) -> bool:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(f"{url}/health")
    return resp.status_code == 200


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict:
    from argus.sandbox.client import RemoteSandbox

    timeout_s = request.app.state.settings.health_check_timeout_s
    database, cache = await asyncio.gather(
        _bounded(_database(request), timeout_s),
        _bounded(request.app.state.cache.backend.ping(), timeout_s),
    )
    checks = {"database": database, "cache": cache}
    ok = all(checks.values())
    if not ok:
        response.status_code = 503
    body: dict = {"status": "ok" if ok else "degraded", "checks": checks}
    sandbox = getattr(getattr(request.app.state, "runs", None), "sandbox", None)
    if isinstance(sandbox, RemoteSandbox):
        body["optional"] = {"sandbox": await _bounded(_sandbox(sandbox.url, timeout_s), timeout_s)}
    return body
