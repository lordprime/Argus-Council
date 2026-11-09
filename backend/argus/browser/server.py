"""Browser service: read-only browser sessions for council agents, behind a token.

Deploy as the ``browser`` container (compose profile ``browser``): non-root, read-only root
filesystem with a tmpfs for Chrome, on an internal network whose only way out is the ``egress``
proxy. The API reaches it over a Unix socket. Every route except ``/health`` needs
``Authorization: Bearer $ARGUS_BROWSER_TOKEN``; with no token configured everything is refused.

Run with ``uvicorn argus.browser.server:app --uds /run/argus-browser/browser.sock``.
``ARGUS_BROWSER_ENGINE=fake`` serves in-memory pages instead of Chrome.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from argus.browser.engine import AgentBrowserEngine, Engine, FakeEngine
from argus.browser.sessions import ActionRefused, BrowserBusy, SessionManager, UnknownSession
from argus.core.logging import configure_logging

log = logging.getLogger(__name__)

REAP_INTERVAL_S = 30.0
# An idle page sends no frames; a comment line every few seconds is what notices a gone viewer.
FRAME_HEARTBEAT_S = 3.0
_ID = r"^[A-Za-z0-9._:-]+$"


_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def configure_logging_from_env(environ: dict[str, str] | None = None) -> str:
    """Log like the API: ``ARGUS_LOG_LEVEL`` (default INFO) and ``ARGUS_LOG_JSON``.

    At DEBUG the CDP guard logs every target it attaches, each command and answer, and each
    paused document request. Returns the level in effect (an unknown name falls back to INFO).
    """
    env = os.environ if environ is None else environ
    level = (env.get("ARGUS_LOG_LEVEL") or "INFO").strip().upper()
    unknown = level not in _LOG_LEVELS
    if unknown:
        level = "INFO"
    json_logs = (env.get("ARGUS_LOG_JSON") or "").strip().lower() in ("1", "true", "yes", "on")
    configure_logging(level, json_logs)
    if unknown:
        log.warning("ARGUS_LOG_LEVEL is not one of %s; logging at INFO", ", ".join(_LOG_LEVELS))
    return level


def _engine_from_env() -> Engine:
    if os.environ.get("ARGUS_BROWSER_ENGINE", "").lower() == "fake":
        return FakeEngine()
    return AgentBrowserEngine.from_env()


def _manager_from_env() -> SessionManager:
    env = os.environ
    return SessionManager(
        _engine_from_env(),
        max_sessions=int(env.get("ARGUS_BROWSER_MAX_SESSIONS", "4")),
        per_run=int(env.get("ARGUS_BROWSER_SESSIONS_PER_RUN", "2")),
        idle_timeout_s=float(env.get("ARGUS_BROWSER_IDLE_TIMEOUT_S", "300")),
    )


class SessionIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=64, pattern=_ID)
    member_id: str = Field(min_length=1, max_length=128, pattern=_ID)
    viewport: tuple[Annotated[int, Field(ge=320, le=3840)], Annotated[int, Field(ge=240, le=2160)]] | None = (
        None
    )


class ActionIn(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    args: dict[str, Any] = Field(default_factory=dict)


class _ClosingStream(StreamingResponse):
    """Always closes the body iterator, however the client went away.

    Starlette leaves an async generator suspended at ``yield`` when the send fails; closing it
    here runs its ``finally`` now, which ends the engine-side stream subscription.
    """

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


async def _sse(frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)

    async def pump() -> None:
        try:
            async for frame in frames:
                await queue.put(frame)
        except Exception as exc:
            log.warning("browser frame stream ended: %s", exc)
        await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), FRAME_HEARTBEAT_S)
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            if frame is None:
                return
            yield b"data: " + base64.b64encode(frame) + b"\n\n"
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        aclose = getattr(frames, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


async def _reaper(manager: SessionManager) -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        try:
            reaped = await manager.reap_idle()
            if reaped:
                log.info("reaped %d idle browser session(s)", reaped)
        except Exception:
            log.exception("browser idle reaper failed")


def create_app(token: str | None = None, manager: SessionManager | None = None) -> FastAPI:
    secret = token if token is not None else os.environ.get("ARGUS_BROWSER_TOKEN", "")
    own_service = manager is None  # tests pass their own manager and keep their own logging
    manager = manager or _manager_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if own_service:
            # At startup, not on import: importing this module must not reconfigure logging.
            level = configure_logging_from_env()
            log.info("browser service logging at %s", level)
        reaper = asyncio.create_task(_reaper(manager))
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(BaseException):
                await reaper
            await manager.close_all()

    app = FastAPI(title="Argus browser", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager

    def auth(authorization: str = Header(default="")) -> None:
        # Refuse everything when no token is configured, and compare in constant time.
        if not secret or not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", status_code=201, dependencies=[Depends(auth)])
    async def open_session(data: SessionIn) -> Any:
        try:
            sid = await manager.open(data.run_id, data.member_id, data.viewport or (1280, 800))
        except BrowserBusy:
            return JSONResponse(status_code=503, content={"error": "busy"})
        except Exception:
            log.exception("browser session failed to start")
            return JSONResponse(status_code=502, content={"error": "start_failed"})
        return {"session_id": sid}

    @app.post("/sessions/{session_id}/actions", dependencies=[Depends(auth)])
    async def act(session_id: str, data: ActionIn) -> dict:
        try:
            result = await manager.act(session_id, data.action, data.args)
        except ActionRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except UnknownSession:
            raise HTTPException(status_code=404, detail="unknown session") from None
        return {
            "url": result.url,
            "title": result.title,
            "snapshot": result.snapshot,
            "screenshot_b64": base64.b64encode(result.screenshot).decode() if result.screenshot else None,
            "error": result.error,
        }

    @app.get("/sessions/{session_id}/frames", dependencies=[Depends(auth)])
    async def frames(session_id: str, max_fps: int = Query(default=4, ge=1, le=30)) -> StreamingResponse:
        try:
            stream = manager.frames(session_id, max_fps)
        except UnknownSession:
            raise HTTPException(status_code=404, detail="unknown session") from None
        return _ClosingStream(
            _sse(stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete("/sessions/{session_id}", status_code=204, dependencies=[Depends(auth)])
    async def close_session(session_id: str) -> Response:
        await manager.close(session_id)
        return Response(status_code=204)

    @app.delete("/runs/{run_id}/sessions", dependencies=[Depends(auth)])
    async def close_run(run_id: str) -> dict:
        return {"closed": await manager.close_run(run_id)}

    return app


app = create_app()
