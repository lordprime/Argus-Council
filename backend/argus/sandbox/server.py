"""Sandbox service: runs commands for council agents inside an isolated container.

Deploy with no network, a read-only root filesystem, a tmpfs work area, dropped capabilities and
CPU/memory/pid limits (see docker-compose). Requests must carry the shared ``ARGUS_SANDBOX_TOKEN``.

With ``ARGUS_SANDBOX_UID_POOL`` set, every execution runs as a uid of its own (see
:mod:`argus.sandbox.isolation`); if the pool is set but cannot be used (the server did not start
as root), every execution is refused rather than run unisolated.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from argus.sandbox.executor import SandboxError, execute
from argus.sandbox.isolation import Isolation, IsolationError

app = FastAPI(title="Argus sandbox", docs_url=None, redoc_url=None, openapi_url=None)
_TOKEN = os.environ.get("ARGUS_SANDBOX_TOKEN", "")
_WORK = os.environ.get("ARGUS_SANDBOX_WORKDIR", "/work")
_WORK_ROOT = _WORK if os.path.isdir(_WORK) else None


def _isolation() -> tuple[Isolation | None, str | None]:
    try:
        return Isolation.from_env(os.environ.get("ARGUS_SANDBOX_UID_POOL"), _WORK_ROOT), None
    except (IsolationError, ValueError) as exc:
        return None, str(exc)


_ISOLATION, _ISOLATION_ERROR = _isolation()


def _secure_socket_dir() -> str | None:
    """Root must own the socket directory so a sandboxed uid can never swap the socket.

    Runs at import, which uvicorn does before it binds ``--uds``. Fixes volumes created by
    older images, where the directory belonged to the API user.
    """
    d = os.environ.get("ARGUS_SANDBOX_SOCKET_DIR")
    if _ISOLATION is None or not d or not os.path.isdir(d):
        return None
    try:
        if os.stat(d).st_uid != 0:
            os.chown(d, 0, 0)
        os.chmod(d, 0o755)  # noqa: S103 - the API user must reach the socket; only root may write
    except OSError as exc:
        return f"cannot secure {d}: {exc}"
    return None


_ISOLATION_ERROR = _ISOLATION_ERROR or _secure_socket_dir()


class ExecIn(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)
    command: str = Field(min_length=1, max_length=10_000)
    timeout_s: int = Field(default=60, ge=1, le=600)
    mem_mb: int = Field(default=1024, ge=64, le=8192)


@app.get("/health")
async def health() -> dict:
    if _ISOLATION_ERROR:
        raise HTTPException(status_code=503, detail=f"isolation unavailable: {_ISOLATION_ERROR}")
    return {"status": "ok", "isolated": _ISOLATION is not None}


@app.post("/exec")
async def exec_(data: ExecIn, authorization: str = Header(default="")) -> dict:
    if not _TOKEN or not hmac.compare_digest(authorization.encode(), f"Bearer {_TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="unauthorized")
    if _ISOLATION_ERROR:  # fail closed: never fall back to running everyone as one uid
        raise HTTPException(status_code=503, detail=f"isolation unavailable: {_ISOLATION_ERROR}")
    try:
        return await execute(
            data.files,
            data.command,
            timeout_s=data.timeout_s,
            mem_mb=data.mem_mb,
            work_root=_WORK_ROOT,
            isolation=_ISOLATION,
        )
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
