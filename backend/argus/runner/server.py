"""Agent runner service: internal HTTP API in front of the vendor CLIs.

Deploy as the ``runner`` container: non-root, no Docker socket, read-only root filesystem, the
per-user HOME volume, reachable only from the API on an internal network (plus egress for the
vendor APIs). Every route except ``/health`` needs ``Authorization: Bearer $ARGUS_RUNNER_TOKEN``.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from argus.runner.process import Runner, RunnerConfig, RunnerError
from argus.runner.specs import UUID_RE, Binaries, CliName, InvokeSpec


def _config_from_env() -> RunnerConfig:
    return RunnerConfig(
        home_root=Path(os.environ.get("ARGUS_RUNNER_HOME_ROOT", "/home/runner/users")),
        binaries=Binaries(
            claude=os.environ.get("ARGUS_RUNNER_CLAUDE_BIN", "claude"),
            codex=os.environ.get("ARGUS_RUNNER_CODEX_BIN", "codex"),
            gemini=os.environ.get("ARGUS_RUNNER_GEMINI_BIN", "gemini"),
        ),
        max_concurrency=int(os.environ.get("ARGUS_RUNNER_MAX_CONCURRENCY", "8")),
        max_per_user=int(os.environ.get("ARGUS_RUNNER_MAX_PER_USER", "4")),
    )


class StatusIn(BaseModel):
    user_id: str
    cli: CliName


class SessionIn(BaseModel):
    user_id: str
    cli: CliName
    session_id: str = Field(max_length=64)


def create_app(token: str | None = None, runner: Runner | None = None) -> FastAPI:
    app = FastAPI(title="Argus agent runner", docs_url=None, redoc_url=None, openapi_url=None)
    secret = token if token is not None else os.environ.get("ARGUS_RUNNER_TOKEN", "")
    app.state.runner = runner or Runner(_config_from_env())

    def auth(authorization: str = Header(default="")) -> None:
        # Refuse everything when no token is configured, and compare in constant time.
        if not secret or not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    def _user(user_id: str) -> str:
        if not UUID_RE.match(user_id):
            raise HTTPException(status_code=422, detail="user_id must be a UUID")
        return user_id.lower()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/clis", dependencies=[Depends(auth)])
    async def clis(refresh: bool = False) -> dict:
        return {"versions": await app.state.runner.versions(refresh=refresh)}

    @app.post("/v1/status", dependencies=[Depends(auth)])
    async def status(data: StatusIn) -> dict:
        user_id = _user(data.user_id)
        return await app.state.runner.status(user_id, data.cli)

    @app.post("/v1/sessions/check", dependencies=[Depends(auth)])
    async def check_session(data: SessionIn) -> dict:
        user_id = _user(data.user_id)
        if not UUID_RE.match(data.session_id):
            return {"exists": False}
        return {"exists": app.state.runner.has_session(user_id, data.cli, data.session_id)}

    @app.get("/v1/users/{user_id}/sessions/{cli}", dependencies=[Depends(auth)])
    async def sessions(user_id: str, cli: CliName) -> dict:
        return {"sessions": app.state.runner.sessions(_user(user_id), cli)}

    @app.delete("/v1/users/{user_id}", dependencies=[Depends(auth)])
    async def purge(user_id: str) -> dict:
        return {"purged": await app.state.runner.purge(_user(user_id))}

    @app.post("/v1/invoke", dependencies=[Depends(auth)])
    async def invoke(body: dict) -> StreamingResponse:
        try:
            spec = InvokeSpec.model_validate(body)
        except ValidationError as exc:
            # Never echo the body back: it carries the prompt and possibly an API key.
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
            raise HTTPException(status_code=422, detail={"invalid": fields}) from None

        async def lines() -> AsyncIterator[bytes]:
            try:
                async for event in app.state.runner.invoke(spec):
                    yield json.dumps(event, separators=(",", ":")).encode() + b"\n"
            except RunnerError as exc:
                yield json.dumps(
                    {"type": "argus.exit", "ok": False, "error": exc.code, "detail": str(exc)}
                ).encode()
                yield b"\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    return app


app = create_app()
