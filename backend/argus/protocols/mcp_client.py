"""MCP client: connect council agents to external MCP servers.

Transports: Streamable HTTP (JSON or SSE responses) and stdio (spawning a
local process; admin-only and disabled unless explicitly allowed).
"""

from __future__ import annotations

import abc
import asyncio
import itertools
import json
import os
import re
from typing import Any

import httpx
import orjson

from argus import __version__
from argus.core.netguard import DEFAULT_MAX_RESPONSE_BYTES, guarded_client

PROTOCOL_VERSION = "2025-06-18"
MAX_RESULT_CHARS = 100_000


class McpClientError(Exception):
    pass


class McpClient(abc.ABC):
    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.server_info: dict[str, Any] = {}

    @abc.abstractmethod
    async def _request(self, method: str, params: dict[str, Any] | None) -> Any: ...

    @abc.abstractmethod
    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "argus-council", "version": __version__},
            },
        )
        self.server_info = result or {}
        await self._notify("notifications/initialized")
        return self.server_info

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):  # pagination guard
            res = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(res.get("tools", []))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def aclose(self) -> None:  # noqa: B027
        pass

    @staticmethod
    def _unwrap(msg: dict[str, Any]) -> Any:
        if "error" in msg:
            e = msg["error"] or {}
            raise McpClientError(f"MCP error {e.get('code')}: {str(e.get('message'))[:300]}")
        return msg.get("result")


class HttpMcpClient(McpClient):
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout)
        self.url = url
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self.session_id: str | None = None
        self.client = client or guarded_client(
            max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES, timeout=httpx.Timeout(timeout, connect=10.0)
        )

    def _hdrs(self) -> dict[str, str]:
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return h

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self.client.post(self.url, content=orjson.dumps(payload), headers=self._hdrs())
        except httpx.HTTPError as exc:
            raise McpClientError(f"MCP server unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400 and resp.status_code != 202:
            raise McpClientError(f"MCP server returned HTTP {resp.status_code}")
        if sid := resp.headers.get("mcp-session-id"):
            self.session_id = sid
        return resp

    async def _request(self, method: str, params: dict[str, Any] | None) -> Any:
        rid = next(self._ids)
        resp = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            for block in resp.text.split("\n\n"):
                data = "".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
                if not data:
                    continue
                msg = json.loads(data)
                if msg.get("id") == rid:
                    return self._unwrap(msg)
            raise McpClientError("No response in event stream")
        return self._unwrap(resp.json())

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._post(payload)

    async def aclose(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(self.url, headers=self._hdrs())
            except httpx.HTTPError:
                pass
        await self.client.aclose()


class StdioMcpClient(McpClient):
    """Newline-delimited JSON-RPC over a child process's stdin/stdout."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None, timeout: float = 60.0) -> None:
        super().__init__(timeout)
        if not command:
            raise McpClientError("stdio transport needs a command")
        self.command = command
        # Minimal environment: never leak the server's own secrets into the child.
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", **(env or {})}  # noqa: S108
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _start(self) -> asyncio.subprocess.Process:
        if self.proc is None:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
            )
        return self.proc

    async def _send(self, payload: dict[str, Any]) -> None:
        proc = await self._start()
        assert proc.stdin is not None
        proc.stdin.write(orjson.dumps(payload) + b"\n")
        await proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any] | None) -> Any:
        rid = next(self._ids)
        async with self._lock:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            proc = await self._start()
            assert proc.stdout is not None
            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise McpClientError(f"MCP stdio timeout on {method}")
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                if not line:
                    raise McpClientError("MCP stdio server exited")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate log noise on stdout
                if msg.get("id") == rid:
                    return self._unwrap(msg)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        async with self._lock:
            await self._send(payload)

    async def aclose(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()


def tool_name(server: str, tool: str) -> str:
    """Namespaced, provider-safe tool name: ``mcp__<server>__<tool>`` (max 64 chars)."""
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"mcp__{server}__{tool}")
    return clean[:64]


def render_result(result: dict[str, Any]) -> tuple[str, bool]:
    parts: list[str] = []
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(str(res.get("text") or f"[resource {res.get('uri')}]"))
        elif kind == "resource_link":
            parts.append(f"[resource link {block.get('uri')}]")
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], default=str))
    text = "\n".join(parts)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[truncated]"
    return text, bool(result.get("isError"))
