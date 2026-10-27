"""External MCP servers, remote A2A/ACP agents, and the protocol ledger."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, SettingsDep, write_scope
from argus.api.schemas import McpServerIn, McpServerPatch, RemoteAgentIn, RemoteAgentPatch
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, Conflict, Forbidden, NotFound
from argus.db.models import LedgerEntry, McpServer, RemoteAgent
from argus.protocols.a2a_client import A2AClient
from argus.protocols.acp_client import AcpClient
from argus.services import ledger
from argus.services.connections import (
    DEFAULT_TRUST,
    agent_requires_approval,
    connect_mcp,
    encode_mcp_risk,
    ledgered,
    mcp_settings,
    public_card,
    set_agent_card,
)
from argus.services.llm import validate_base_url

router = APIRouter(tags=["connections"], dependencies=[write_scope("connections:write")])


class UpstreamError(ArgusError):
    status_code = 502
    code = "upstream_error"


def _mcp_out(s: McpServer, box: SecretBox | None = None) -> dict[str, Any]:
    header_names = sorted(json.loads(box.decrypt(s.headers_enc))) if box and s.headers_enc else []
    trust, require_approval, risk = mcp_settings(s)
    return {
        "id": str(s.id),
        "name": s.name,
        "transport": s.transport,
        "url": s.url,
        "command": s.command,
        "header_names": header_names,
        "tool_allow": s.tool_allow,
        "risk": risk,
        "trust": trust,
        "require_approval": require_approval,
        "enabled": s.enabled,
        "tools": [
            {"name": t.get("name"), "description": t.get("description", "")[:300]} for t in s.last_tools or []
        ],
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _agent_out(a: RemoteAgent) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "protocol": a.protocol,
        "url": a.url,
        "agent_name": a.agent_name,
        "description": a.description,
        "enabled": a.enabled,
        "requires_approval": agent_requires_approval(a),
        "has_token": bool(a.token_enc),
        "card": public_card(a),
        "created_at": a.created_at.isoformat(),
    }


async def _get(db, model, owner_id: uuid.UUID, obj_id: uuid.UUID):
    obj = await db.get(model, obj_id)
    if obj is None or obj.owner_id != owner_id:
        raise NotFound("Not found")
    return obj


async def _unique(db, model, owner_id: uuid.UUID, name: str) -> None:
    if (await db.execute(select(model.id).where(model.owner_id == owner_id, model.name == name))).first():
        raise Conflict(f"'{name}' already exists")


# ---------------------------------------------------------------- MCP servers
@router.get("/mcp-servers")
async def list_mcp(principal: CurrentPrincipal, db: DbDep, settings: SettingsDep) -> dict:
    box = SecretBox(settings)
    rows = await db.execute(
        select(McpServer).where(McpServer.owner_id == principal.id).order_by(McpServer.name)
    )
    return {"servers": [_mcp_out(s, box) for s in rows.scalars()], "stdio_enabled": settings.mcp_allow_stdio}


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
async def create_mcp(
    data: McpServerIn, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    principal.require_scope("connections:write")
    await _unique(db, McpServer, principal.id, data.name)
    url = None
    if data.transport == "stdio":
        if not settings.mcp_allow_stdio:
            raise Forbidden("stdio MCP servers are disabled on this deployment")
        if not principal.user.is_admin:
            raise Forbidden("Only administrators can register stdio MCP servers")
        if not data.command:
            raise ArgusError("command is required for stdio servers")
    else:
        if not data.url:
            raise ArgusError("url is required for HTTP MCP servers")
        url = validate_base_url(data.url, settings.provider_allow_private_urls)
    box = SecretBox(settings)
    server = McpServer(
        owner_id=principal.id,
        name=data.name,
        transport=data.transport,
        url=url,
        command=data.command if data.transport == "stdio" else [],
        headers_enc=box.encrypt(json.dumps(data.headers)) if data.headers else None,
        tool_allow=data.tool_allow,
        risk=encode_mcp_risk(data.risk, data.trust or DEFAULT_TRUST, data.require_approval),
        enabled=data.enabled,
    )
    db.add(server)
    await db.flush()
    return _mcp_out(server, box)


@router.patch("/mcp-servers/{server_id}")
async def update_mcp(
    server_id: uuid.UUID, data: McpServerPatch, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    server = await _get(db, McpServer, principal.id, server_id)
    box = SecretBox(settings)
    values = data.model_dump(exclude_unset=True)
    if "headers" in values:
        h = values.pop("headers")
        server.headers_enc = box.encrypt(json.dumps(h)) if h else None
    if values.get("url"):
        values["url"] = validate_base_url(values["url"], settings.provider_allow_private_urls)
    trust, require_approval, risk = mcp_settings(server)
    if {"risk", "trust", "require_approval"} & values.keys():
        new = {k: values.pop(k, None) for k in ("risk", "trust", "require_approval")}
        server.risk = encode_mcp_risk(
            risk if new["risk"] is None else new["risk"],
            new["trust"] or trust,
            require_approval if new["require_approval"] is None else new["require_approval"],
        )
    for k, v in values.items():
        setattr(server, k, v)
    await db.flush()
    return _mcp_out(server, box)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp(server_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    await db.delete(await _get(db, McpServer, principal.id, server_id))


@router.post("/mcp-servers/{server_id}/discover")
async def discover_mcp(
    server_id: uuid.UUID, request: Request, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    server = await _get(db, McpServer, principal.id, server_id)
    box = SecretBox(settings)
    try:
        client, tools = await connect_mcp(
            server, settings, box, sessionmaker=request.app.state.db.sessionmaker
        )
    except Exception as exc:
        raise UpstreamError(f"Could not connect: {str(exc)[:200]}") from exc
    info = client.server_info
    await client.aclose()
    server.last_tools = tools
    return {**_mcp_out(server, box), "server_info": info.get("serverInfo", {})}


# ---------------------------------------------------------------- remote agents
@router.get("/remote-agents")
async def list_agents(principal: CurrentPrincipal, db: DbDep) -> dict:
    rows = await db.execute(
        select(RemoteAgent).where(RemoteAgent.owner_id == principal.id).order_by(RemoteAgent.name)
    )
    return {"agents": [_agent_out(a) for a in rows.scalars()]}


@router.post("/remote-agents", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: RemoteAgentIn, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    principal.require_scope("connections:write")
    await _unique(db, RemoteAgent, principal.id, data.name)
    agent = RemoteAgent(
        owner_id=principal.id,
        name=data.name,
        protocol=data.protocol,
        url=validate_base_url(data.url, settings.provider_allow_private_urls),
        agent_name=data.agent_name,
        token_enc=SecretBox(settings).encrypt(data.token) if data.token else None,
        description=data.description,
        enabled=data.enabled,
    )
    set_agent_card(agent, {}, requires_approval=data.requires_approval)
    db.add(agent)
    await db.flush()
    return _agent_out(agent)


@router.patch("/remote-agents/{agent_id}")
async def update_agent(
    agent_id: uuid.UUID, data: RemoteAgentPatch, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    agent = await _get(db, RemoteAgent, principal.id, agent_id)
    values = data.model_dump(exclude_unset=True)
    if "token" in values:
        t = values.pop("token")
        agent.token_enc = SecretBox(settings).encrypt(t) if t else None
    if values.get("url"):
        values["url"] = validate_base_url(values["url"], settings.provider_allow_private_urls)
    if "requires_approval" in values:
        set_agent_card(agent, requires_approval=bool(values.pop("requires_approval")))
    if "agent_name" in values:
        name = values.pop("agent_name")
        if agent.protocol != "acp":
            raise ArgusError("agent_name only applies to ACP agents")
        if not name:
            raise ArgusError("agent_name is required for ACP agents")
        if name != agent.agent_name:
            agent.agent_name = name
            set_agent_card(agent, {})  # the cached card described the previous agent
    for k, v in values.items():
        setattr(agent, k, v)
    await db.flush()
    return _agent_out(agent)


@router.delete("/remote-agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    await db.delete(await _get(db, RemoteAgent, principal.id, agent_id))


@router.post("/remote-agents/{agent_id}/refresh")
async def refresh_agent(
    agent_id: uuid.UUID, request: Request, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
) -> dict:
    """Fetch the A2A agent card, or confirm the ACP agent exists, and cache it (both ledgered)."""
    agent = await _get(db, RemoteAgent, principal.id, agent_id)
    token = SecretBox(settings).decrypt(agent.token_enc) if agent.token_enc else None
    maker = request.app.state.db.sessionmaker
    detail = {"agent": agent.name, "purpose": "refresh"}
    try:
        if agent.protocol == "a2a":
            client = A2AClient(agent.url, token, timeout=settings.protocol_timeout_s)
            try:
                base = agent.url.rsplit("/", 1)[0] if agent.url.rstrip("/").endswith("/a2a") else agent.url
                card = await ledgered(
                    maker,
                    principal.id,
                    lambda: client.fetch_card(base),
                    protocol="a2a",
                    peer=base,
                    method="agent-card",
                    request={"base_url": base},
                    detail=detail,
                )
            finally:
                await client.aclose()
        else:
            client2 = AcpClient(agent.url, token, timeout=settings.protocol_timeout_s)
            try:
                listing = await ledgered(
                    maker,
                    principal.id,
                    client2.agents,
                    protocol="acp",
                    peer=agent.url,
                    method="agents:list",
                    request={"path": "/agents"},
                    detail=detail,
                )
            finally:
                await client2.aclose()
            found = {a.get("name"): a for a in listing}
            if agent.agent_name not in found:
                raise UpstreamError(f"ACP server has no agent named '{agent.agent_name}'")
            card = found[agent.agent_name]
    except ArgusError:
        raise
    except Exception as exc:
        raise UpstreamError(f"Could not reach agent: {str(exc)[:200]}") from exc
    set_agent_card(agent, card if isinstance(card, dict) else {})
    if not agent.description:
        agent.description = str(public_card(agent).get("description", ""))[:2000]
    return _agent_out(agent)


# ---------------------------------------------------------------- ledger
@router.get("/ledger")
async def list_ledger(
    principal: CurrentPrincipal,
    db: DbDep,
    protocol: str | None = None,
    run_id: uuid.UUID | None = None,
    purpose: ledger.Purpose | None = None,
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Newest first. Every filter is applied before ``limit``; page with ``next_before_seq``."""
    q = select(LedgerEntry).where(LedgerEntry.owner_id == principal.id)
    if protocol:
        q = q.where(LedgerEntry.protocol == protocol)
    if run_id:
        q = q.where(LedgerEntry.run_id == run_id)
    if purpose:
        q = q.where(ledger.purpose_is(purpose))
    if before_seq:
        q = q.where(LedgerEntry.seq < before_seq)
    entries = list((await db.execute(q.order_by(LedgerEntry.seq.desc()).limit(limit))).scalars())
    return {
        "next_before_seq": entries[-1].seq if len(entries) == limit else None,
        "entries": [
            {
                "seq": e.seq,
                "ts": e.ts.isoformat(),
                "protocol": e.protocol,
                "direction": e.direction,
                "peer": e.peer,
                "method": e.method,
                "status": e.status,
                "latency_ms": e.latency_ms,
                "run_id": str(e.run_id) if e.run_id else None,
                "request_sha256": e.request_sha256,
                "response_sha256": e.response_sha256,
                "hash": e.hash,
                "prev_hash": e.prev_hash,
                "detail": e.detail,
            }
            for e in entries
        ],
    }


@router.get("/ledger/verify")
async def verify_ledger(principal: CurrentPrincipal, db: DbDep) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    return await ledger.verify(maker, principal.id)
