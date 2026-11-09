"""Client for the browser service (read-only live browsing in a separate, egress-confined container)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx


class BrowserBusy(Exception):
    """The service has no free session slot."""


class SessionLost(Exception):
    """The service does not know the session (it restarted, or closed it as idle)."""


class RemoteBrowser:
    """``url`` is ``http(s)://host:port`` or ``unix:/path/to/socket`` (as for the sandbox client)."""

    def __init__(
        self, url: str, token: str, client: httpx.AsyncClient | None = None, *, timeout_s: float = 60.0
    ) -> None:
        self.uds: str | None = None
        if url.startswith("unix:"):
            self.uds = url[len("unix:") :]
            url = "http://browser"
        self.url = url.rstrip("/")
        self.token = token
        self.client = client
        self.timeout_s = timeout_s

    def _client(self, read_timeout: float | None = None) -> httpx.AsyncClient:
        timeout = httpx.Timeout(read_timeout or self.timeout_s, connect=5.0)
        if self.uds:
            return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.uds), timeout=timeout)
        return httpx.AsyncClient(timeout=timeout)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        client = self.client or self._client()
        try:
            return await client.request(method, f"{self.url}{path}", headers=self._headers, **kw)
        finally:
            if self.client is None:
                await client.aclose()

    async def open_session(self, run_id: str, member_id: str) -> str:
        resp = await self._request("POST", "/sessions", json={"run_id": run_id, "member_id": member_id})
        if resp.status_code == 503:
            raise BrowserBusy()
        if resp.status_code != 201:
            raise RuntimeError(f"Browser service returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()["session_id"]

    async def act(self, session_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request(
            "POST", f"/sessions/{session_id}/actions", json={"action": action, "args": args}
        )
        if resp.status_code == 404:
            raise SessionLost()
        if resp.status_code != 200:
            raise RuntimeError(f"Browser service returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def frames(self, session_id: str, max_fps: int) -> AsyncIterator[bytes]:
        """Live JPEG frames (decoded from the service's SSE ``data:`` lines) until the stream ends."""
        import base64

        client = self.client or self._client()
        client_timeout = httpx.Timeout(None, connect=5.0)
        try:
            async with client.stream(
                "GET",
                f"{self.url}/sessions/{session_id}/frames",
                params={"max_fps": max_fps},
                headers=self._headers,
                timeout=client_timeout,
            ) as resp:
                if resp.status_code == 404:
                    raise SessionLost()
                if resp.status_code != 200:
                    raise RuntimeError(f"Browser service returned HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload:
                            with contextlib.suppress(ValueError):
                                yield base64.b64decode(payload)
        finally:
            if self.client is None:
                await client.aclose()

    async def close_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/sessions/{session_id}")

    async def close_run(self, run_id: str) -> None:
        await self._request("DELETE", f"/runs/{run_id}/sessions")
