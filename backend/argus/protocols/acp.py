"""Agent Communication Protocol (ACP): REST agents and runs, server and client.

Server routes live under ``/acp``: ``GET /agents``, ``GET /agents/{name}``,
``POST /runs`` (sync or async), ``GET /runs/{id}``, ``POST /runs/{id}/cancel``.
ACP agents map to Argus modes (``argus-research`` ...) and to your saved
councils (``council-<name>``).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep
from argus.api.schemas import RunIn
from argus.core.errors import ArgusError, NotFound
from argus.db.models import Council, Run
from argus.services import ledger
from argus.services.runs import create_and_start_run, wait_terminal

router = APIRouter(prefix="/acp", tags=["acp"])

_STATUS = {
    "queued": "created",
    "running": "in-progress",
    "paused": "awaiting",
    "completed": "completed",
    "failed": "failed",
    "halted": "failed",
    "cancelled": "cancelled",
}
MODE_AGENTS = {
    "argus-research": ("research", "Research council: investigates a topic and writes a cited report."),
    "argus-coding": ("coding", "Coding council: designs, implements and tests software."),
    "argus-chat": ("chat", "Chat council: answers questions conversationally."),
    "argus-general": ("general", "General council: plans and produces any requested output."),
}


class MessagePart(BaseModel):
    content_type: str = "text/plain"
    content: str | None = Field(default=None, max_length=200_000)
    content_url: str | None = None
    name: str | None = None


class AcpMessage(BaseModel):
    role: str = "user"
    parts: list[MessagePart] = Field(min_length=1, max_length=50)


class RunCreate(BaseModel):
    agent_name: str = Field(max_length=120)
    input: list[AcpMessage] = Field(min_length=1, max_length=50)
    mode: Literal["sync", "async", "stream"] = "sync"
    session_id: str | None = None


def _manifest(name: str, description: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_content_types": ["text/plain", "text/markdown"],
        "output_content_types": ["text/markdown", "text/plain"],
        "metadata": {"framework": "argus-council", **(metadata or {})},
    }


async def _agents(db, owner_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    out = {n: _manifest(n, d) for n, (_, d) in MODE_AGENTS.items()}
    rows = await db.execute(select(Council).where(Council.owner_id == owner_id))
    for c in rows.scalars():
        name = f"council-{c.name.lower().replace(' ', '-')}"
        out[name] = _manifest(
            name,
            c.description or f"Saved council '{c.name}' ({c.mode})",
            {"council_id": str(c.id), "mode": c.mode},
        )
    return out


def run_view(run: Run, agent_name: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "agent_name": agent_name,
        "run_id": str(run.id),
        "session_id": None,
        "status": _STATUS.get(run.status, "in-progress"),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "output": [],
        "error": {"code": "server_error", "message": run.error} if run.error else None,
    }
    if run.result:
        out["output"] = [
            {
                "role": f"agent/{agent_name}",
                "parts": [{"content_type": "text/markdown", "content": run.result}],
            }
        ]
    return out


@router.get("/ping")
async def ping() -> dict:
    return {}


@router.get("/agents")
async def list_agents(principal: CurrentPrincipal, db: DbDep) -> dict:
    return {"agents": list((await _agents(db, principal.id)).values())}


@router.get("/agents/{name}")
async def get_agent(name: str, principal: CurrentPrincipal, db: DbDep) -> dict:
    agents = await _agents(db, principal.id)
    if name not in agents:
        raise NotFound("Agent not found")
    return agents[name]


@router.post("/runs")
async def create_run(data: RunCreate, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    principal.require_scope("runs:write")
    if data.mode == "stream":
        raise ArgusError("stream mode is not supported; use sync or async")
    agents = await _agents(db, principal.id)
    manifest = agents.get(data.agent_name)
    if manifest is None:
        raise NotFound("Agent not found")
    text = "\n\n".join(p.content or "" for m in data.input for p in m.parts if p.content).strip()
    if not text:
        raise ArgusError("input must contain text content")
    topic, _, rest = text.partition("\n")
    meta = manifest["metadata"]
    council_id = uuid.UUID(meta["council_id"]) if "council_id" in meta else None
    mode = meta.get("mode") or MODE_AGENTS[data.agent_name][0]
    t0 = time.monotonic()
    run = await create_and_start_run(
        db,
        request.app.state.runs,
        principal.id,
        RunIn(topic=topic[:500], description=(rest or text)[:20_000], mode=mode, council_id=council_id),
    )
    if data.mode == "sync":
        run = await wait_terminal(request.app.state.db.sessionmaker, run.id, 600)
    view = run_view(run, data.agent_name)
    view["session_id"] = data.session_id
    await ledger.record(
        request.app.state.db.sessionmaker,
        owner_id=principal.id,
        protocol="acp",
        direction="inbound",
        peer=request.headers.get("user-agent", "acp-client")[:200],
        method=f"runs:{data.mode}",
        status=view["status"],
        request=data.model_dump(),
        response=view,
        latency_ms=int((time.monotonic() - t0) * 1000),
        run_id=run.id,
    )
    return view


async def _own(db, owner_id: uuid.UUID, run_id: str) -> Run:
    try:
        rid = uuid.UUID(run_id)
    except ValueError as exc:
        raise NotFound("Run not found") from exc
    run = await db.get(Run, rid)
    if run is None or run.owner_id != owner_id:
        raise NotFound("Run not found")
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: CurrentPrincipal, db: DbDep) -> dict:
    run = await _own(db, principal.id, run_id)
    return run_view(run, f"argus-{run.mode}")


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    run = await _own(db, principal.id, run_id)
    await request.app.state.runs.cancel(run.id)
    view = run_view(run, f"argus-{run.mode}")
    view["status"] = "cancelling"
    return view
