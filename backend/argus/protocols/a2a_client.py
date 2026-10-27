"""A2A client: talk to remote Agent2Agent agents (JSON-RPC over HTTP)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import orjson

from argus.protocols.jsonrpc import RpcError


def message_text(message: dict[str, Any]) -> str:
    return "\n".join(
        str(p.get("text", "")) for p in message.get("parts") or [] if p.get("kind", "text") == "text"
    )


class A2AClient:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def fetch_card(self, base_url: str) -> dict[str, Any]:
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            resp = await self.client.get(base_url.rstrip("/") + path)
            if resp.status_code == 200:
                return resp.json()
        raise RpcError(-1, "Agent card not found")

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        resp = await self.client.post(self.url, content=orjson.dumps(payload), headers=self.headers)
        if resp.status_code >= 400:
            raise RpcError(resp.status_code, f"A2A peer returned HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            raise RpcError(data["error"].get("code", -1), str(data["error"].get("message"))[:300])
        return data.get("result")

    async def send(self, text: str, metadata: dict[str, Any] | None = None, blocking: bool = False) -> dict:
        message = {
            "kind": "message",
            "role": "user",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": text}],
            "metadata": metadata or {},
        }
        return await self._rpc("message/send", {"message": message, "configuration": {"blocking": blocking}})

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._rpc("tasks/get", {"id": task_id})

    async def cancel(self, task_id: str) -> dict[str, Any]:
        return await self._rpc("tasks/cancel", {"id": task_id})

    async def run_to_completion(
        self, text: str, metadata: dict[str, Any] | None = None, timeout_s: float = 600, poll_s: float = 2.0
    ) -> dict[str, Any]:
        result = await self.send(text, metadata)
        if result.get("kind") != "task":
            return result  # a direct message reply
        deadline = asyncio.get_running_loop().time() + timeout_s
        while result["status"]["state"] in ("submitted", "working"):
            if asyncio.get_running_loop().time() > deadline:
                await self.cancel(result["id"])
                raise RpcError(-1, "Remote A2A task timed out")
            await asyncio.sleep(poll_s)
            result = await self.get_task(result["id"])
        return result

    async def aclose(self) -> None:
        await self.client.aclose()


def task_text(result: dict[str, Any]) -> str:
    if result.get("kind") == "message":
        return message_text(result)
    texts = [message_text(a) for a in result.get("artifacts") or []]
    status_msg = (result.get("status") or {}).get("message")
    if not texts and status_msg:
        texts.append(message_text(status_msg))
    return "\n\n".join(t for t in texts if t) or f"(task {result.get('status', {}).get('state')}, no output)"
