"""Attach a user's external MCP servers and remote agents to a run's tool registry.

Per-connection settings that have no column of their own live in existing JSON columns so no
migration is needed:

* MCP trust level and per-tool approvals are stored as reserved tags in ``McpServer.risk``
  (``trust:<level>`` / ``approve:<tool>``); :func:`mcp_settings` splits them back out.
* A remote agent's ``requires_approval`` flag is kept in ``RemoteAgent.card`` under
  :data:`AGENT_SETTINGS_KEY` and survives card refreshes.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.logging import get_logger
from argus.core.netguard import guarded_client
from argus.db.models import McpServer, RemoteAgent
from argus.protocols.mcp_client import HttpMcpClient, McpClient, StdioMcpClient
from argus.services import ledger
from argus.tools.base import ToolRegistry
from argus.tools.remote import McpTool, RemoteAgentTool

log = get_logger(__name__)

TRUST_LEVELS = ("trusted", "ask", "blocked")
DEFAULT_TRUST = "ask"  # for newly registered servers
LEGACY_TRUST = "trusted"  # rows created before trust levels existed behave exactly as before
# Risk tag added to tools that need a human OK; it is in the default ``approval_required`` list,
# so the existing approval gate fires for it.
APPROVAL_RISK = "network_write"
_TRUST_TAG = "trust:"
_APPROVE_TAG = "approve:"
AGENT_SETTINGS_KEY = "x-argus"


# ---------------------------------------------------------------- stored settings
def mcp_settings(server: McpServer) -> tuple[str, list[str], list[str]]:
    """Return ``(trust, require_approval, risk)`` decoded from the server's risk column."""
    trust, approve, risk = LEGACY_TRUST, [], []
    for tag in server.risk or []:
        tag = str(tag)
        if tag.startswith(_TRUST_TAG) and tag[len(_TRUST_TAG) :] in TRUST_LEVELS:
            trust = tag[len(_TRUST_TAG) :]
        elif tag.startswith(_APPROVE_TAG):
            approve.append(tag[len(_APPROVE_TAG) :])
        else:
            risk.append(tag)
    return trust, approve, risk


def encode_mcp_risk(risk: list[str], trust: str, require_approval: list[str]) -> list[str]:
    clean = [r for r in dict.fromkeys(risk) if not r.startswith((_TRUST_TAG, _APPROVE_TAG))]
    return [*clean, f"{_TRUST_TAG}{trust}", *(f"{_APPROVE_TAG}{t}" for t in dict.fromkeys(require_approval))]


def mcp_tool_risk(server: McpServer, tool: str) -> list[str]:
    """Risk tags a tool from ``server`` carries in a run (``ask`` adds the approval tag)."""
    trust, approve, risk = mcp_settings(server)
    if (trust == "ask" or tool in approve) and APPROVAL_RISK not in risk:
        return [*risk, APPROVAL_RISK]
    return risk


def agent_requires_approval(agent: RemoteAgent) -> bool:
    return bool(((agent.card or {}).get(AGENT_SETTINGS_KEY) or {}).get("requires_approval", True))


def public_card(agent: RemoteAgent) -> dict[str, Any]:
    return {k: v for k, v in (agent.card or {}).items() if k != AGENT_SETTINGS_KEY}


def set_agent_card(agent: RemoteAgent, card: dict[str, Any] | None = None, **settings: Any) -> None:
    """Replace the cached card and/or update settings, preserving the other part."""
    stored = dict((agent.card or {}).get(AGENT_SETTINGS_KEY) or {})
    stored.update(settings)
    agent.card = {**(public_card(agent) if card is None else card), AGENT_SETTINGS_KEY: stored}


# ---------------------------------------------------------------- ledger
async def ledgered[T](
    sessionmaker: async_sessionmaker | None,
    owner_id: uuid.UUID | None,
    call: Callable[[], Awaitable[T]],
    *,
    protocol: str,
    peer: str,
    method: str,
    request: Any,
    run_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> T:
    """Run an outbound protocol call and append it to the owner's ledger (success or failure)."""
    t0 = time.monotonic()
    status, response = "ok", None
    try:
        result = await call()
        response = result
        return result
    except BaseException as exc:
        status, response = "error", {"error": str(exc)[:300] or type(exc).__name__}
        raise
    finally:
        if sessionmaker is not None and owner_id is not None:
            try:
                await ledger.record(
                    sessionmaker,
                    owner_id=owner_id,
                    protocol=protocol,
                    direction="outbound",
                    peer=peer,
                    method=method,
                    status=status,
                    request=request,
                    response=json.loads(json.dumps(response, default=str)) if response is not None else None,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    run_id=run_id,
                    detail=detail,
                )
            except Exception as exc:  # the ledger must never break the call it observes
                log.warning("ledger.record_failed", method=method, error=str(exc)[:200])


# ---------------------------------------------------------------- MCP
def mcp_peer(server: McpServer) -> str:
    return server.url or f"stdio:{(server.command or ['?'])[0]}"


def build_mcp_client(server: McpServer, settings: Settings, box: SecretBox) -> McpClient:
    headers: dict[str, str] = json.loads(box.decrypt(server.headers_enc)) if server.headers_enc else {}
    if server.transport == "stdio":
        if not settings.mcp_allow_stdio:
            raise PermissionError("stdio MCP servers are disabled (ARGUS_MCP_ALLOW_STDIO=false)")
        return StdioMcpClient(
            [str(c) for c in server.command], env=headers, timeout=settings.protocol_timeout_s
        )
    if not server.url:
        raise ValueError("HTTP MCP server needs a url")
    return HttpMcpClient(
        server.url,
        headers=headers,
        timeout=settings.protocol_timeout_s,
        client=outbound_client(settings, settings.protocol_timeout_s),
    )


def outbound_client(settings: Settings, timeout: float) -> httpx.AsyncClient:
    """SSRF-guarded client for user-configured peers (MCP servers, remote agents)."""
    return guarded_client(
        settings.connections_allow_private_urls, timeout=httpx.Timeout(timeout, connect=10.0)
    )


async def connect_mcp(
    server: McpServer,
    settings: Settings,
    box: SecretBox,
    *,
    sessionmaker: async_sessionmaker | None = None,
    run_id: uuid.UUID | None = None,
    purpose: str = "discover",
) -> tuple[McpClient, list[dict]]:
    """Initialize and list tools; both exchanges are ledgered when ``sessionmaker`` is given."""
    client = build_mcp_client(server, settings, box)
    peer, detail = mcp_peer(server), {"server": server.name, "purpose": purpose}

    def call[R](method: str, fn: Callable[[], Awaitable[R]], request: Any) -> Awaitable[R]:
        return ledgered(
            sessionmaker,
            server.owner_id,
            lambda: asyncio.wait_for(fn(), settings.protocol_timeout_s),
            protocol="mcp",
            peer=peer,
            method=method,
            request=request,
            run_id=run_id,
            detail=detail,
        )

    try:
        await call("initialize", client.initialize, {"method": "initialize"})
        tools = await call("tools/list", client.list_tools, {"method": "tools/list"})
    except BaseException:
        await client.aclose()
        raise
    allowed = [
        t for t in tools if any(fnmatch.fnmatchcase(t.get("name", ""), p) for p in server.tool_allow or ["*"])
    ]
    return client, allowed


# ---------------------------------------------------------------- remote agents
class GatedRemoteAgentTool(RemoteAgentTool):
    """``remote_agent`` whose approval risk depends on the target agent's ``requires_approval``."""

    def risk_for(self, args: BaseModel) -> frozenset[str]:
        agent = self.agents.get(getattr(args, "agent", ""))
        if agent is not None and not agent.get("requires_approval", True):
            return self.risk - {"remote_agent"}
        return self.risk


# ---------------------------------------------------------------- run wiring
async def attach_connections(
    registry: ToolRegistry,
    sessionmaker: async_sessionmaker,
    owner_id: uuid.UUID,
    settings: Settings,
    emit: Any,
) -> list[Any]:
    """Register MCP and remote-agent tools; returns async closers to run when the run ends."""
    box = SecretBox(settings)
    closers: list[Any] = []
    run_id = getattr(getattr(emit, "__self__", None), "run_id", None)
    async with sessionmaker() as s:
        servers = list(
            (
                await s.execute(
                    select(McpServer).where(McpServer.owner_id == owner_id, McpServer.enabled.is_(True))
                )
            ).scalars()
        )
        agents = list(
            (
                await s.execute(
                    select(RemoteAgent).where(RemoteAgent.owner_id == owner_id, RemoteAgent.enabled.is_(True))
                )
            ).scalars()
        )
    # Blocked servers are never contacted, so their tools never reach a run.
    servers = [srv for srv in servers if mcp_settings(srv)[0] != "blocked"]

    async def one(server: McpServer) -> None:
        try:
            client, tools = await connect_mcp(
                server, settings, box, sessionmaker=sessionmaker, run_id=run_id, purpose="run"
            )
        except Exception as exc:
            log.warning("mcp.connect_failed", server=server.name, error=str(exc)[:200])
            await emit(
                "error",
                {
                    "code": "mcp_unavailable",
                    "message": f"MCP server '{server.name}' unavailable",
                    "details": {"server": server.name},
                },
            )
            return
        closers.append(client.aclose)
        peer = mcp_peer(server)
        for spec in tools:
            if "name" in spec:
                risk = mcp_tool_risk(server, spec["name"])
                registry.register(McpTool(server.name, client, spec, risk, peer))

    await asyncio.gather(*(one(s) for s in servers))
    if agents:
        table: dict[str, dict[str, Any]] = {}
        for a in agents:
            table[a.name] = {
                "protocol": a.protocol,
                "url": a.url,
                "agent_name": a.agent_name,
                "description": a.description,
                "token": box.decrypt(a.token_enc) if a.token_enc else None,
                "requires_approval": agent_requires_approval(a),
                "allow_private": settings.connections_allow_private_urls,
            }
        registry.register(GatedRemoteAgentTool(table, timeout_s=settings.remote_agent_timeout_s))
    return closers
