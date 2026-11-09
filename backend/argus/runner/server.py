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

from argus.runner.login import LoginError
from argus.runner.process import Runner, RunnerConfig, RunnerError
from argus.runner.specs import UUID_RE, Binaries, CliName, InvokeSpec, session_ok


def _config_from_env() -> RunnerConfig:
    return RunnerConfig(
        home_root=Path(os.environ.get("ARGUS_RUNNER_HOME_ROOT", "/home/runner/users")),
        binaries=Binaries(
            claude=os.environ.get("ARGUS_RUNNER_CLAUDE_BIN", "claude"),
            codex=os.environ.get("ARGUS_RUNNER_CODEX_BIN", "codex"),
            gemini=os.environ.get("ARGUS_RUNNER_GEMINI_BIN", "gemini"),
            copilot=os.environ.get("ARGUS_RUNNER_COPILOT_BIN", "copilot"),
            kiro=os.environ.get("ARGUS_RUNNER_KIRO_BIN", "kiro-cli"),
            qwen=os.environ.get("ARGUS_RUNNER_QWEN_BIN", "qwen"),
            opencode=os.environ.get("ARGUS_RUNNER_OPENCODE_BIN", "opencode"),
            goose=os.environ.get("ARGUS_RUNNER_GOOSE_BIN", "goose"),
            vibe=os.environ.get("ARGUS_RUNNER_VIBE_BIN", "vibe"),
        ),
        max_concurrency=int(os.environ.get("ARGUS_RUNNER_MAX_CONCURRENCY", "8")),
        max_per_user=int(os.environ.get("ARGUS_RUNNER_MAX_PER_USER", "4")),
    )


class StatusIn(BaseModel):
    user_id: str
    cli: CliName


class LoginStartIn(StatusIn):
    method: str | None = Field(default=None, max_length=32, pattern=r"^[a-z_]+$")


class LoginCodeIn(StatusIn):
    code: str = Field(max_length=600)


class SessionIn(BaseModel):
    user_id: str
    cli: CliName
    session_id: str = Field(max_length=64)


def create_app(token: str | None = None, runner: Runner | None = None) -> FastAPI:
    app = FastAPI(title="Argus agent runner", docs_url=None, redoc_url=None, openapi_url=None)
    secret = token if token is not None else os.environ.get("ARGUS_RUNNER_TOKEN", "")
    app.state.runner = runner or Runner(_config_from_env())

    async def _stop_logins() -> None:
        await app.state.runner.logins.close()

    app.router.on_shutdown.append(_stop_logins)

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

    def _refused(exc: LoginError) -> HTTPException:
        status = 429 if exc.code == "busy" else 409
        return HTTPException(status_code=status, detail={"error": exc.code, "message": exc.message})

    @app.post("/v1/login/start", dependencies=[Depends(auth)])
    async def login_start(data: LoginStartIn) -> dict:
        try:
            return await app.state.runner.logins.start(_user(data.user_id), data.cli, data.method)
        except LoginError as exc:
            raise _refused(exc) from None

    @app.post("/v1/login/state", dependencies=[Depends(auth)])
    async def login_state(data: StatusIn) -> dict:
        return app.state.runner.logins.state(_user(data.user_id), data.cli)

    @app.post("/v1/login/code", dependencies=[Depends(auth)])
    async def login_code(body: dict) -> dict:
        try:
            data = LoginCodeIn.model_validate(body)
        except ValidationError:
            # Never echo the body back: it carries the code.
            raise HTTPException(status_code=422, detail={"error": "invalid_code"}) from None
        try:
            return await app.state.runner.logins.submit_code(_user(data.user_id), data.cli, data.code)
        except ValueError as exc:
            detail = {"error": "invalid_code", "message": str(exc)}
            raise HTTPException(status_code=422, detail=detail) from None
        except LoginError as exc:
            raise _refused(exc) from None

    @app.post("/v1/login/cancel", dependencies=[Depends(auth)])
    async def login_cancel(data: StatusIn) -> dict:
        return await app.state.runner.logins.cancel(_user(data.user_id), data.cli)

    @app.post("/v1/logout", dependencies=[Depends(auth)])
    async def logout(data: StatusIn) -> dict:
        return await app.state.runner.logout(_user(data.user_id), data.cli)

    @app.post("/v1/sessions/check", dependencies=[Depends(auth)])
    async def check_session(data: SessionIn) -> dict:
        user_id = _user(data.user_id)
        if not session_ok(data.cli, data.session_id):
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
                    {"type": "argus.exit", "ok": False, "error": exc.code, "detail": exc.message}
                ).encode()
                yield b"\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    return app


app = create_app()
