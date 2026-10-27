"""ACP client: run agents on remote Agent Communication Protocol servers."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from argus.core.errors import ArgusError
from argus.core.netguard import guarded_client


class AcpClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.client = client or guarded_client(timeout=httpx.Timeout(timeout, connect=10.0))

    async def _req(self, method: str, path: str, json: Any = None) -> Any:
        resp = await self.client.request(method, self.base + path, json=json, headers=self.headers)
        if resp.status_code >= 400:
            raise ArgusError(f"ACP peer returned HTTP {resp.status_code}")
        return resp.json()

    async def agents(self) -> list[dict[str, Any]]:
        return (await self._req("GET", "/agents")).get("agents", [])

    async def run(self, agent_name: str, text: str, timeout_s: float = 600, poll_s: float = 2.0) -> dict:
        body = {
            "agent_name": agent_name,
            "mode": "async",
            "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": text}]}],
        }
        run = await self._req("POST", "/runs", body)
        deadline = asyncio.get_running_loop().time() + timeout_s
        while run.get("status") in ("created", "in-progress"):
            if asyncio.get_running_loop().time() > deadline:
                await self._req("POST", f"/runs/{run['run_id']}/cancel")
                raise ArgusError("Remote ACP run timed out")
            await asyncio.sleep(poll_s)
            run = await self._req("GET", f"/runs/{run['run_id']}")
        return run

    async def aclose(self) -> None:
        await self.client.aclose()


def run_text(run: dict[str, Any]) -> str:
    texts = [p.get("content") or "" for m in run.get("output") or [] for p in m.get("parts") or []]
    if not any(texts) and run.get("error"):
        return f"(remote run {run.get('status')}: {run['error'].get('message')})"
    return "\n\n".join(t for t in texts if t) or f"(remote run {run.get('status')}, no output)"
