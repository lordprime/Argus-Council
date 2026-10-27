"""Argus as an MCP server (Streamable HTTP transport, JSON responses).

Tools let any MCP client (Claude Code, Claude Desktop, IDEs, other agents)
start and steer council runs. Run deliverables are exposed as resources and
skills as prompts. Every call is recorded in the protocol ledger.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import orjson
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select

from argus import __version__
from argus.api.deps import Principal, resolve_principal
from argus.api.schemas import RunIn
from argus.core.errors import ArgusError
from argus.db.models import Artifact, Council, Run
from argus.protocols.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcError,
    err,
    ok,
    validate_request,
)
from argus.services import ledger
from argus.services.runs import create_and_start_run, wait_for_run, wait_seconds
from argus.skills.library import skills_for_owner

router = APIRouter(tags=["mcp"])

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
MAX_WAIT_S = 600

_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "What the council should work on"},
        "description": {"type": "string", "description": "Details, constraints and expectations"},
        "mode": {"type": "string", "enum": ["general", "research", "coding", "chat"]},
        "council_id": {"type": "string", "description": "Saved council to use (optional)"},
        "provider": {"type": "string"},
        "model": {"type": "string"},
        "outputs": {"type": "array", "items": {"type": "string"}},
        "guardrails": {"type": "object"},
        "wait_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_WAIT_S,
            "description": "Block until the run finishes (0 = return immediately)",
        },
    },
    "required": ["topic"],
    "additionalProperties": False,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "start_run",
        "title": "Start a council run",
        "description": "Start an autonomous Argus council run on a topic (research, coding, chat, general).",
        "inputSchema": _RUN_SCHEMA,
    },
    {
        "name": "get_run",
        "title": "Get run status and result",
        "description": "Get a run's status, usage and final deliverable.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_runs",
        "title": "List recent runs",
        "description": "List your most recent runs.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
    },
    {
        "name": "intervene",
        "title": "Steer a running council",
        "description": "Send a message or correction to a running council (kind: message | correct).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["message", "correct"]},
                "text": {"type": "string"},
                "scope": {"type": "string", "enum": ["member", "council"]},
                "member_id": {"type": "string"},
            },
            "required": ["run_id", "kind", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_run",
        "title": "Cancel a run",
        "description": "Cancel a running council.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_councils",
        "title": "List councils",
        "description": "List your saved councils.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _text(obj: Any, is_error: bool = False) -> dict[str, Any]:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str, indent=2)
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(obj, dict) and not is_error:
        out["structuredContent"] = json.loads(json.dumps(obj, default=str))
    return out


def _run_view(run: Run) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "status": run.status,
        "topic": run.topic,
        "mode": run.mode,
        "usage": run.usage,
        "error": run.error,
        "result": run.result or None,
    }


async def _own_run(db, principal: Principal, raw_id: Any) -> Run:
    try:
        run_id = uuid.UUID(str(raw_id))
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, "run_id must be a UUID") from exc
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != principal.id:
        raise RpcError(INVALID_PARAMS, "Run not found")
    return run


async def _call_tool(request: Request, db, principal: Principal, name: str, args: dict[str, Any]) -> dict:
    state = request.app.state
    if name == "start_run":
        principal.require_scope("runs:write")
        wait = min(wait_seconds(args.pop("wait_seconds", 0) or 0, 0.0), MAX_WAIT_S)
        try:
            data = RunIn.model_validate(args)
        except ValidationError as exc:
            return _text(f"Invalid arguments: {exc.errors(include_url=False)[:3]}", True)
        run = await create_and_start_run(db, state.runs, principal.id, data)
        if wait > 0:
            run = await wait_for_run(state, run, wait, protocol="mcp")
        return _text(_run_view(run))
    if name == "get_run":
        run = await _own_run(db, principal, args.get("run_id"))
        await db.refresh(run)
        return _text(_run_view(run))
    if name == "list_runs":
        limit = max(1, min(int(args.get("limit") or 10), 50))
        rows = await db.execute(
            select(Run).where(Run.owner_id == principal.id).order_by(Run.created_at.desc()).limit(limit)
        )
        return _text(
            {
                "runs": [
                    {"run_id": str(r.id), "topic": r.topic, "status": r.status, "mode": r.mode}
                    for r in rows.scalars()
                ]
            }
        )
    if name == "intervene":
        principal.require_scope("runs:write")
        run = await _own_run(db, principal, args.get("run_id"))
        cmd = {
            "kind": args["kind"],
            "text": args.get("text"),
            "scope": args.get("scope"),
            "target": {"member_id": args["member_id"]} if args.get("member_id") else {},
        }
        return _text(await state.runs.intervene(run.id, cmd, user_name="MCP client"))
    if name == "cancel_run":
        principal.require_scope("runs:write")
        run = await _own_run(db, principal, args.get("run_id"))
        await state.runs.cancel(run.id)
        return _text({"run_id": str(run.id), "status": "cancelling"})
    if name == "list_councils":
        rows = await db.execute(
            select(Council).where(Council.owner_id == principal.id).order_by(Council.name)
        )
        return _text(
            {"councils": [{"id": str(c.id), "name": c.name, "mode": c.mode} for c in rows.scalars()]}
        )
    raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")


async def _dispatch(request: Request, db, principal: Principal, method: str, params: dict[str, Any]) -> Any:
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in SUPPORTED_VERSIONS else LATEST,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "argus-council", "title": "Argus Council", "version": __version__},
            "instructions": "Start autonomous agent council runs with start_run, poll with get_run, "
            "steer with "
            "intervene. Deliverables are available as resources; skills are available as prompts.",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "tools/call needs name and arguments")
        try:
            return await _call_tool(request, db, principal, name, dict(args))
        except ArgusError as exc:
            return _text(exc.message, True)
    if method == "resources/list":
        rows = await db.execute(
            select(Artifact.run_id, Artifact.path, Artifact.mime)
            .join(Run, Run.id == Artifact.run_id)
            .where(Run.owner_id == principal.id, Artifact.deleted.is_(False))
            .distinct()
            .limit(500)
        )
        return {
            "resources": [
                {"uri": f"argus://runs/{r.run_id}/artifacts/{r.path}", "name": r.path, "mimeType": r.mime}
                for r in rows
            ]
        }
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        prefix = "argus://runs/"
        if not uri.startswith(prefix) or "/artifacts/" not in uri:
            raise RpcError(INVALID_PARAMS, "Unknown resource URI")
        run_part, path = uri[len(prefix) :].split("/artifacts/", 1)
        run = await _own_run(db, principal, run_part)
        art = (
            await db.execute(
                select(Artifact)
                .where(Artifact.run_id == run.id, Artifact.path == path, Artifact.deleted.is_(False))
                .order_by(Artifact.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if art is None:
            raise RpcError(INVALID_PARAMS, "Resource not found")
        return {"contents": [{"uri": uri, "mimeType": art.mime, "text": art.content or ""}]}
    if method == "prompts/list":
        skills = await skills_for_owner(request.app.state.db.sessionmaker, principal.id)
        return {
            "prompts": [
                {
                    "name": p.name,
                    "description": p.description,
                    "arguments": [
                        {"name": "task", "description": "What to apply the skill to", "required": False}
                    ],
                }
                for p in skills.values()
            ]
        }
    if method == "prompts/get":
        skills = await skills_for_owner(request.app.state.db.sessionmaker, principal.id)
        pkg = skills.get(str(params.get("name")))
        if pkg is None:
            raise RpcError(INVALID_PARAMS, "Unknown prompt")
        task = (params.get("arguments") or {}).get("task")
        text = pkg.body + (f"\n\n## Task\n{task}" if task else "")
        return {
            "description": pkg.description,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    state = request.app.state
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key")
    if not token:
        return JSONResponse(
            err(None, -32001, "Authentication required"),
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    async with state.db.sessionmaker() as db:
        try:
            principal = await resolve_principal(db, state.settings, token, state.cache)
        except ArgusError:
            return JSONResponse(
                err(None, -32001, "Invalid credentials"),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        await db.commit()
        if await state.cache.hit_rate_limit(f"user:{principal.id}", state.settings.rate_limit_per_minute):
            return JSONResponse(err(None, -32002, "Rate limited"), status_code=429)
        raw = await request.body()
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return JSONResponse(err(None, PARSE_ERROR, "Parse error"), status_code=400)
        if isinstance(msg, list):
            return JSONResponse(err(None, -32600, "Batch requests are not supported"), status_code=400)
        t0 = time.monotonic()
        try:
            id_, method, params, is_notification = validate_request(msg)
        except RpcError as exc:
            return JSONResponse(
                err(msg.get("id") if isinstance(msg, dict) else None, exc.code, exc.message), status_code=400
            )
        if is_notification:
            return Response(status_code=202)
        try:
            result = await _dispatch(request, db, principal, method, params)
            await db.commit()
            body, status = ok(id_, result), "ok"
        except RpcError as exc:
            body, status = err(id_, exc.code, exc.message, exc.data), "error"
        except Exception:
            body, status = err(id_, INTERNAL_ERROR, "Internal error"), "error"
    if method not in ("ping",):
        await ledger.record(
            state.db.sessionmaker,
            owner_id=principal.id,
            protocol="mcp",
            direction="inbound",
            peer=request.headers.get("user-agent", "mcp-client")[:200],
            method=method if method != "tools/call" else f"tools/call:{params.get('name')}",
            status=status,
            request=msg,
            response=body,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
    headers = {}
    if method == "initialize":
        headers["Mcp-Session-Id"] = uuid.uuid4().hex
    return JSONResponse(body, headers=headers)


@router.get("/mcp")
async def mcp_get() -> Response:
    # Server-initiated SSE streams are not offered; per the spec, respond 405.
    return Response(status_code=405, headers={"Allow": "POST"})
