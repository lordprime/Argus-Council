"""Sandbox service: runs commands for council agents inside an isolated container.

Deploy with no outbound network, a read-only root filesystem, a tmpfs work
area, dropped capabilities and CPU/memory/pid limits (see docker-compose).
Requests must carry the shared ``ARGUS_SANDBOX_TOKEN``.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from argus.sandbox.executor import SandboxError, execute

app = FastAPI(title="Argus sandbox", docs_url=None, redoc_url=None, openapi_url=None)
_TOKEN = os.environ.get("ARGUS_SANDBOX_TOKEN", "")
_WORK = os.environ.get("ARGUS_SANDBOX_WORKDIR", "/work")
_WORK_ROOT = _WORK if os.path.isdir(_WORK) else None


class ExecIn(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)
    command: str = Field(min_length=1, max_length=10_000)
    timeout_s: int = Field(default=60, ge=1, le=600)
    mem_mb: int = Field(default=1024, ge=64, le=8192)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/exec")
async def exec_(data: ExecIn, authorization: str = Header(default="")) -> dict:
    if not _TOKEN or not hmac.compare_digest(authorization.encode(), f"Bearer {_TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        return await execute(
            data.files,
            data.command,
            timeout_s=data.timeout_s,
            mem_mb=data.mem_mb,
            work_root=_WORK_ROOT,
        )
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
