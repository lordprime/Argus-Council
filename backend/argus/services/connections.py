"""Attach a user's external MCP servers and remote agents to a run's tool registry."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.logging import get_logger
from argus.db.models import McpServer, RemoteAgent
from argus.protocols.mcp_client import HttpMcpClient, McpClient, StdioMcpClient
from argus.tools.base import ToolRegistry
from argus.tools.remote import McpTool, RemoteAgentTool

log = get_logger(__name__)


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
    return HttpMcpClient(server.url, headers=headers, timeout=settings.protocol_timeout_s)


async def connect_mcp(server: McpServer, settings: Settings, box: SecretBox) -> tuple[McpClient, list[dict]]:
    client = build_mcp_client(server, settings, box)
    try:
        await asyncio.wait_for(client.initialize(), settings.protocol_timeout_s)
        tools = await asyncio.wait_for(client.list_tools(), settings.protocol_timeout_s)
    except BaseException:
        await client.aclose()
        raise
    allowed = [
        t for t in tools if any(fnmatch.fnmatchcase(t.get("name", ""), p) for p in server.tool_allow or ["*"])
    ]
    return client, allowed


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

    async def one(server: McpServer) -> None:
        try:
            client, tools = await connect_mcp(server, settings, box)
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
        peer = server.url or f"stdio:{(server.command or ['?'])[0]}"
        for spec in tools:
            if "name" in spec:
                registry.register(McpTool(server.name, client, spec, list(server.risk or []), peer))

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
            }
        registry.register(RemoteAgentTool(table, timeout_s=settings.remote_agent_timeout_s))
    return closers
