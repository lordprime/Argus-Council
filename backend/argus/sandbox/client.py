"""Client for the sandbox service, with an in-process fallback for local development."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from argus.sandbox.executor import execute


class Sandbox(Protocol):
    async def run(self, files: dict[str, str], command: str, timeout_s: int) -> dict[str, Any]: ...


class RemoteSandbox:
    def __init__(self, url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.client = client

    async def run(self, files: dict[str, str], command: str, timeout_s: int) -> dict[str, Any]:
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s + 30, connect=5.0))
        try:
            resp = await client.post(
                f"{self.url}/exec",
                json={"files": files, "command": command, "timeout_s": timeout_s},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code != 200:
            raise RuntimeError(f"Sandbox returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


class LocalSandbox:
    """Runs commands on this host with rlimits only. Development use; not an isolation boundary."""

    async def run(self, files: dict[str, str], command: str, timeout_s: int) -> dict[str, Any]:
        return await execute(files, command, timeout_s=timeout_s)


def build_sandbox(settings: Any) -> Sandbox | None:
    if settings.sandbox_url and settings.sandbox_token:
        return RemoteSandbox(settings.sandbox_url, settings.sandbox_token.get_secret_value())
    if settings.sandbox_local and not settings.is_production:
        return LocalSandbox()
    return None
