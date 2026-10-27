"""Agent2Agent (A2A) protocol: Argus as an A2A agent, plus a client for remote A2A agents.

Server: agent card at ``/.well-known/agent-card.json`` (and the legacy
``/.well-known/agent.json``), JSON-RPC at ``/a2a`` with ``message/send``,
``tasks/get`` and ``tasks/cancel``. Each A2A task is an Argus run.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from argus import __version__
from argus.api.deps import resolve_principal
from argus.api.schemas import RunIn
from argus.core.errors import ArgusError
from argus.db.models import Run
from argus.protocols.a2a_client import message_text
from argus.protocols.jsonrpc import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcError,
    err,
    ok,
    validate_request,
)
from argus.services import ledger
from argus.services.runs import create_and_start_run, wait_terminal

router = APIRouter(tags=["a2a"])

PROTOCOL_VERSION = "0.3.0"
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
MODES = ("research", "coding", "chat", "general")

_STATE = {
    "queued": "submitted",
    "running": "working",
    "paused": "input-required",
    "completed": "completed",
    "failed": "failed",
    "halted": "failed",
    "cancelled": "canceled",
}


def agent_card(base_url: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Argus Council",
        "description": "Autonomous, guarded multi-agent councils for research, coding and chat. "
        "Send a topic; "
        "a council plans, executes with oversight, and returns the deliverable.",
        "url": f"{base_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/markdown", "text/plain"],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": "research",
                "name": "Research",
                "tags": ["research", "report", "citations"],
                "description": "Investigate a topic and write a cited report.",
                "examples": ["Research the state of solid-state batteries in 2026"],
            },
            {
                "id": "coding",
                "name": "Coding",
                "tags": ["code", "software"],
                "description": "Design, implement and test software in a sandboxed workspace.",
                "examples": ["Build a CLI that converts CSV to Parquet"],
            },
            {
                "id": "chat",
                "name": "Chat",
                "tags": ["qa"],
                "description": "Answer questions conversationally.",
                "examples": ["Explain Rust ownership"],
            },
        ],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def task_from_run(run: Run, context_id: str | None = None) -> dict[str, Any]:
    state = _STATE.get(run.status, "working")
    task: dict[str, Any] = {
        "kind": "task",
        "id": str(run.id),
        "contextId": context_id or str(run.id),
        "status": {"state": state, "timestamp": _now()},
        "metadata": {"argus": {"status": run.status, "usage": run.usage}},
    }
    if run.error:
        task["status"]["message"] = {
            "kind": "message",
            "role": "agent",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": run.error}],
        }
    if run.result:
        task["artifacts"] = [
            {
                "artifactId": f"{run.id}-deliverable",
                "name": "deliverable.md",
                "parts": [{"kind": "text", "text": run.result}],
            }
        ]
    return task


FORBIDDEN = -32010  # application error: authenticated but not allowed


async def _owned_run(db, owner_id: uuid.UUID, task_id: Any) -> Run:
    try:
        rid = uuid.UUID(str(task_id))
    except ValueError as exc:
        raise RpcError(TASK_NOT_FOUND, "Task not found") from exc
    run = await db.get(Run, rid)
    if run is None or run.owner_id != owner_id:
        raise RpcError(TASK_NOT_FOUND, "Task not found")
    return run


MAX_BLOCKING_S = 600.0


def _wait_seconds(raw: Any) -> float:
    """Client-chosen blocking wait, bounded so one request cannot hold a worker indefinitely."""
    try:
        value = float(raw if raw is not None else MAX_BLOCKING_S)
    except (TypeError, ValueError):
        value = MAX_BLOCKING_S
    return min(max(value, 0.0), MAX_BLOCKING_S) if value == value else MAX_BLOCKING_S


async def _dispatch(request: Request, db, principal, method: str, params: dict[str, Any]) -> Any:
    state = request.app.state
    if method in ("message/send", "tasks/cancel"):
        # Starting or cancelling a task changes state: same scope (and viewer rule) as POST /runs.
        try:
            principal.require_scope("runs:write")
        except ArgusError as exc:
            raise RpcError(FORBIDDEN, exc.message) from None
    if method == "message/send":
        message = params.get("message")
        if not isinstance(message, dict) or not message_text(message).strip():
            raise RpcError(INVALID_PARAMS, "message with at least one text part is required")
        text = message_text(message).strip()
        meta = message.get("metadata") or params.get("metadata") or {}
        mode = meta.get("mode") if meta.get("mode") in MODES else "general"
        topic, _, description = text.partition("\n")
        run = await create_and_start_run(
            db,
            state.runs,
            principal.id,
            RunIn(
                topic=topic[:500],
                description=(description or text)[:20_000],
                mode=mode,
                provider=meta.get("provider", "mock"),
                model=meta.get("model", "mock-1"),
            ),
        )
        cfg = params.get("configuration") or {}
        if cfg.get("blocking"):
            run = await wait_terminal(state.db.sessionmaker, run.id, _wait_seconds(meta.get("timeout_s")))
        return task_from_run(run, message.get("contextId"))
    if method == "tasks/get":
        run = await _owned_run(db, principal.id, params.get("id"))
        return task_from_run(run)
    if method == "tasks/cancel":
        run = await _owned_run(db, principal.id, params.get("id"))
        if run.status in ("completed", "failed", "halted", "cancelled"):
            raise RpcError(TASK_NOT_CANCELABLE, "Task cannot be canceled")
        await state.runs.cancel(run.id)
        await asyncio.sleep(0)
        await db.refresh(run)
        task = task_from_run(run)
        task["status"]["state"] = "canceled"
        return task
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


@router.get("/.well-known/agent-card.json")
@router.get("/.well-known/agent.json")
async def well_known_card(request: Request) -> dict[str, Any]:
    return agent_card(str(request.base_url).rstrip("/"))


@router.post("/a2a")
async def a2a_endpoint(request: Request) -> JSONResponse:
    state = request.app.state
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse(
            err(None, -32003, "Authentication required"),
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    t0 = time.monotonic()
    async with state.db.sessionmaker() as db:
        try:
            principal = await resolve_principal(db, state.settings, auth[7:].strip())
        except ArgusError:
            return JSONResponse(err(None, -32003, "Invalid credentials"), status_code=401)
        await db.commit()
        if await state.cache.hit_rate_limit(f"user:{principal.id}", state.settings.rate_limit_per_minute):
            return JSONResponse(err(None, -32002, "Rate limited"), status_code=429)
        try:
            msg = orjson.loads(await request.body())
        except orjson.JSONDecodeError:
            return JSONResponse(err(None, PARSE_ERROR, "Parse error"), status_code=400)
        try:
            id_, method, params, _ = validate_request(msg)
        except RpcError as exc:
            return JSONResponse(err(None, exc.code, exc.message), status_code=400)
        try:
            body, status = ok(id_, await _dispatch(request, db, principal, method, params)), "ok"
            await db.commit()
        except RpcError as exc:
            body, status = err(id_, exc.code, exc.message), "error"
        except ArgusError as exc:
            body, status = err(id_, INVALID_PARAMS, exc.message), "error"
    result = body.get("result") or {}
    await ledger.record(
        state.db.sessionmaker,
        owner_id=principal.id,
        protocol="a2a",
        direction="inbound",
        peer=request.headers.get("user-agent", "a2a-client")[:200],
        method=method,
        status=status,
        request=msg,
        response=body,
        latency_ms=int((time.monotonic() - t0) * 1000),
        run_id=uuid.UUID(result["id"]) if isinstance(result, dict) and result.get("kind") == "task" else None,
    )
    return JSONResponse(body)
