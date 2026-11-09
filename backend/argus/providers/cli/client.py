"""Client for the agent runner service, with an in-process fallback for local development.

Mirrors ``argus.sandbox.client``: production talks to the ``runner`` container over the internal
network with a shared bearer token; ``ARGUS_RUNNER_LOCAL=true`` runs the CLIs on this host for
development and is refused in production.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import httpx

from argus.runner.specs import InvokeSpec


class RunnerUnavailable(Exception):
    pass


class RunnerRefused(Exception):
    """The runner refused a sign-in request (busy, nothing to answer, bad code, unknown method)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class RunnerTransport(Protocol):
    def invoke(self, spec: InvokeSpec) -> AsyncIterator[dict[str, Any]]: ...

    async def versions(self, refresh: bool = False) -> dict[str, str | None]: ...

    async def status(self, user_id: str, cli: str) -> dict[str, Any]: ...

    async def has_session(self, user_id: str, cli: str, session_id: str) -> bool: ...

    async def sessions(self, user_id: str, cli: str) -> list[dict[str, Any]]: ...

    async def purge(self, user_id: str) -> bool: ...

    async def login_start(self, user_id: str, cli: str, method: str | None = None) -> dict[str, Any]: ...

    async def login_state(self, user_id: str, cli: str) -> dict[str, Any]: ...

    async def login_code(self, user_id: str, cli: str, code: str) -> dict[str, Any]: ...

    async def login_cancel(self, user_id: str, cli: str) -> dict[str, Any]: ...

    async def logout(self, user_id: str, cli: str) -> dict[str, Any]: ...


def _body(spec: InvokeSpec) -> dict[str, Any]:
    body = spec.model_dump(mode="json", exclude={"api_key"})
    if spec.api_key is not None:
        body["api_key"] = spec.api_key.get_secret_value()
    return body


class RemoteRunner:
    """``url`` is ``http(s)://host:port`` or ``unix:/path/to/socket``.

    The compose deployment uses a Unix socket on a shared volume (like the sandbox), so the API has
    no network route to the runner and the runner exposes no port.
    """

    def __init__(self, url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self.uds: str | None = None
        if url.startswith("unix:"):
            self.uds = url[len("unix:") :]
            url = "http://runner"
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self.client = client

    def _client(self, timeout: float) -> httpx.AsyncClient:
        if self.client is not None:
            return self.client
        limits = httpx.Timeout(timeout, connect=5.0)
        if self.uds:
            return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.uds), timeout=limits)
        return httpx.AsyncClient(timeout=limits)

    async def invoke(self, spec: InvokeSpec) -> AsyncIterator[dict[str, Any]]:
        client = self._client(spec.timeout_s + 30)
        try:
            async with client.stream(
                "POST", f"{self.url}/v1/invoke", json=_body(spec), headers=self._headers
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise RunnerUnavailable(f"runner returned HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except httpx.HTTPError as exc:
            raise RunnerUnavailable(f"runner unreachable: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()

    async def _call(self, method: str, path: str, json_body: Any = None, wait_s: float = 30) -> Any:
        client = self._client(wait_s)
        try:
            resp = await client.request(method, f"{self.url}{path}", json=json_body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise RunnerUnavailable(f"runner unreachable: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code in (409, 422, 429):
            try:
                detail = resp.json().get("detail") or {}
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}
            raise RunnerRefused(
                resp.status_code,
                str(detail.get("error") or "refused")[:40],
                str(detail.get("message") or "The runner refused the request.")[:300],
            )
        if resp.status_code != 200:
            raise RunnerUnavailable(f"runner returned HTTP {resp.status_code}")
        return resp.json()

    async def versions(self, refresh: bool = False) -> dict[str, str | None]:
        data = await self._call("GET", f"/v1/clis?refresh={'true' if refresh else 'false'}", wait_s=90)
        return data.get("versions") or {}

    async def status(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/status", {"user_id": user_id, "cli": cli})

    async def has_session(self, user_id: str, cli: str, session_id: str) -> bool:
        data = await self._call(
            "POST", "/v1/sessions/check", {"user_id": user_id, "cli": cli, "session_id": session_id}
        )
        return bool(data.get("exists"))

    async def sessions(self, user_id: str, cli: str) -> list[dict[str, Any]]:
        data = await self._call("GET", f"/v1/users/{user_id}/sessions/{cli}")
        return list(data.get("sessions") or [])

    async def purge(self, user_id: str) -> bool:
        data = await self._call("DELETE", f"/v1/users/{user_id}")
        return bool(data.get("purged"))

    async def login_start(self, user_id: str, cli: str, method: str | None = None) -> dict[str, Any]:
        return await self._call("POST", "/v1/login/start", {"user_id": user_id, "cli": cli, "method": method})

    async def login_state(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/login/state", {"user_id": user_id, "cli": cli})

    async def login_code(self, user_id: str, cli: str, code: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/login/code", {"user_id": user_id, "cli": cli, "code": code})

    async def login_cancel(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/login/cancel", {"user_id": user_id, "cli": cli})

    async def logout(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/logout", {"user_id": user_id, "cli": cli}, wait_s=60)


class LocalRunner:
    """Runs the CLIs on this host (development only; not an isolation boundary)."""

    def __init__(self, home_root: str) -> None:
        from argus.runner.process import Runner, RunnerConfig

        self._runner = Runner(RunnerConfig(home_root=Path(home_root)))

    async def invoke(self, spec: InvokeSpec) -> AsyncIterator[dict[str, Any]]:
        async for event in self._runner.invoke(spec):
            yield event

    async def versions(self, refresh: bool = False) -> dict[str, str | None]:
        return await self._runner.versions(refresh=refresh)

    async def status(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._runner.status(user_id, cli)

    async def has_session(self, user_id: str, cli: str, session_id: str) -> bool:
        return self._runner.has_session(user_id, cli, session_id)

    async def sessions(self, user_id: str, cli: str) -> list[dict[str, Any]]:
        return self._runner.sessions(user_id, cli)

    async def purge(self, user_id: str) -> bool:
        return await self._runner.purge(user_id)

    async def login_start(self, user_id: str, cli: str, method: str | None = None) -> dict[str, Any]:
        from argus.runner.login import LoginError

        try:
            return await self._runner.logins.start(user_id, cli, method)
        except LoginError as exc:
            raise RunnerRefused(429 if exc.code == "busy" else 409, exc.code, exc.message) from None

    async def login_state(self, user_id: str, cli: str) -> dict[str, Any]:
        return self._runner.logins.state(user_id, cli)

    async def login_code(self, user_id: str, cli: str, code: str) -> dict[str, Any]:
        from argus.runner.login import LoginError

        try:
            return await self._runner.logins.submit_code(user_id, cli, code)
        except ValueError as exc:
            raise RunnerRefused(422, "invalid_code", str(exc)) from None
        except LoginError as exc:
            raise RunnerRefused(409, exc.code, exc.message) from None

    async def login_cancel(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._runner.logins.cancel(user_id, cli)

    async def logout(self, user_id: str, cli: str) -> dict[str, Any]:
        return await self._runner.logout(user_id, cli)


def build_runner(settings: Any) -> RunnerTransport | None:
    if settings.runner_url and settings.runner_token:
        return RemoteRunner(settings.runner_url, settings.runner_token.get_secret_value())
    if settings.runner_local and not settings.is_production:
        return LocalRunner(str(Path(settings.data_dir) / "runner-homes"))
    return None
