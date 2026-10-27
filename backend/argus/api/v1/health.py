from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from argus import __version__

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict:
    checks: dict[str, bool] = {}
    try:
        async with request.app.state.db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    checks["cache"] = await request.app.state.cache.backend.ping()
    ok = all(checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "checks": checks}
