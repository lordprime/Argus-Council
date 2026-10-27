"""Tools backed by other systems: external MCP servers and remote A2A / ACP agents."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from argus.core.logging import get_logger
from argus.protocols.a2a_client import A2AClient, task_text
from argus.protocols.acp_client import AcpClient, run_text
from argus.protocols.mcp_client import McpClient, render_result, tool_name
from argus.providers.types import ToolSpec
from argus.runtime.events import Actor
from argus.services import ledger
from argus.tools.base import Tool, ToolCallContext, ToolResult

log = get_logger(__name__)


async def record_call(ctx: ToolCallContext, **entry: Any) -> None:
    """Append an outbound protocol call to the ledger and announce it on the run's live stream.

    The stream event carries the ledger entry's id and hash so the UI can draw the exchange as it
    happens and link it to the tamper-evident record.
    """
    row = await ledger.record(ctx.run.sessionmaker, owner_id=ctx.run.owner_id, run_id=ctx.run.run_id, **entry)
    await ctx.run.sink.emit(
        "protocol.call",
        {
            "ledger_id": row.id,
            "hash": row.hash,
            "protocol": entry["protocol"],
            "direction": entry["direction"],
            "peer": entry["peer"],
            "method": entry["method"],
            "status": entry["status"],
            "latency_ms": entry.get("latency_ms", 0),
            "tool_call_id": ctx.tool_call_id,
            "detail": entry.get("detail") or {},
        },
        actor=Actor(kind="agent", member_id=ctx.member_id, name=ctx.member_name, role=ctx.role),
        step_id=ctx.step_id,
    )


class McpTool(Tool):
    source = "mcp"

    class Args(BaseModel):
        model_config = {"extra": "allow"}

    def __init__(
        self, server: str, client: McpClient, spec: dict[str, Any], risk: list[str], peer: str
    ) -> None:
        self.server = server
        self.client = client
        self.remote_name = spec["name"]
        self.name = tool_name(server, spec["name"])
        self.description = f"[MCP:{server}] {spec.get('description') or spec['name']}"[:1024]
        self.input_schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        self.risk = frozenset(risk)
        self.peer = peer

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)

    async def invoke(self, raw_args: dict[str, Any], ctx: ToolCallContext) -> ToolResult:
        return await self.run(raw_args, ctx)

    async def run(self, args: dict[str, Any], ctx: ToolCallContext) -> ToolResult:
        t0 = time.monotonic()
        status, response = "ok", None
        try:
            response = await self.client.call_tool(self.remote_name, args)
            text, is_error = render_result(response)
            status = "error" if is_error else "ok"
            return ToolResult(text, is_error=is_error)
        except Exception as exc:
            status, response = "error", {"error": str(exc)[:300]}
            return ToolResult.error(f"MCP tool {self.remote_name} on {self.server} failed: {str(exc)[:300]}")
        finally:
            await record_call(
                ctx,
                protocol="mcp",
                direction="outbound",
                peer=self.peer,
                method=f"tools/call:{self.remote_name}",
                status=status,
                request={"name": self.remote_name, "arguments": args},
                response=response,
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail={"server": self.server, "member": ctx.member_name},
            )


class RemoteAgentTool(Tool):
    name = "remote_agent"
    risk = frozenset({"remote_agent"})
    source = "a2a"

    class Args(BaseModel):
        agent: str = Field(max_length=40, description="Name of the remote agent")
        message: str = Field(max_length=50_000, description="Full, self-contained task for the remote agent")

    def __init__(self, agents: dict[str, dict[str, Any]], timeout_s: float = 600) -> None:
        self.agents = agents
        self.timeout_s = timeout_s
        listing = "; ".join(
            f"{n} ({a['protocol']}): {a.get('description') or 'remote agent'}"[:200]
            for n, a in sorted(agents.items())
        )
        self.description = (
            "Delegate a self-contained task to a remote agent over A2A/ACP and get its answer. "
            f"Available: {listing}"
        )[:1024]

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        agent = self.agents.get(args.agent)
        if agent is None:
            return ToolResult.error(
                f"Unknown remote agent '{args.agent}'. Available: {', '.join(self.agents)}"
            )
        t0 = time.monotonic()
        status, response = "ok", None
        try:
            if agent["protocol"] == "a2a":
                client = A2AClient(agent["url"], agent.get("token"))
                try:
                    response = await client.run_to_completion(args.message, timeout_s=self.timeout_s)
                finally:
                    await client.aclose()
                text = task_text(response)
                status = (
                    response.get("status", {}).get("state", "ok") if response.get("kind") == "task" else "ok"
                )
            else:
                client2 = AcpClient(agent["url"], agent.get("token"))
                try:
                    response = await client2.run(agent["agent_name"], args.message, timeout_s=self.timeout_s)
                finally:
                    await client2.aclose()
                text, status = run_text(response), response.get("status", "ok")
            return ToolResult(text, is_error=status in ("failed", "canceled", "cancelled"))
        except Exception as exc:
            status, response = "error", {"error": str(exc)[:300]}
            return ToolResult.error(f"Remote agent '{args.agent}' failed: {str(exc)[:300]}")
        finally:
            await record_call(
                ctx,
                protocol=agent["protocol"],
                direction="outbound",
                peer=agent["url"],
                method="message/send" if agent["protocol"] == "a2a" else "runs:async",
                status=str(status)[:16],
                request={"agent": args.agent, "message": args.message},
                response=json.loads(json.dumps(response, default=str)) if response else None,
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail={"agent": args.agent, "member": ctx.member_name, "call_id": str(uuid.uuid4())},
            )
