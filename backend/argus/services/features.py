"""Feature switches: optional features an operator installs once and an admin turns on and off.

Two levels:

* **Installed** (server side): the deploy scripts start the feature's containers and set its
  settings (``ARGUS_BROWSER_URL``; ``ARGUS_CLI_AGENTS_ENABLED`` plus a runner). The settings stay
  the ceiling: the in-app switch can only turn on what the server allows.
* **On/off** (in the app, no restart): one row per feature in ``feature_switches``. No row means
  on, so installing is the opt-in and deployments that already run a feature keep it.

The switch is read synchronously from an in-memory cache (``is_on``), reloaded at startup, at
each run start and whenever an admin changes it; tool calls and CLI turns consult it every time,
so turning a feature off reaches runs already in progress.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.core.logging import get_logger
from argus.db.base import utcnow
from argus.db.models import FeatureSwitch

log = get_logger(__name__)

FeatureKey = Literal["browser", "cli_agents"]
FeatureStatus = Literal["reachable", "unreachable", "not_installed"]

FEATURE_KEYS: tuple[FeatureKey, ...] = ("browser", "cli_agents")
FEATURE_LABELS: dict[str, str] = {"browser": "Live browser", "cli_agents": "CLI agents"}
INSTALL_COMMANDS: dict[str, str] = {
    "browser": "./scripts/deploy.sh --with browser",
    "cli_agents": "./scripts/deploy.sh --with cli-agents",
}
BROWSER_OFF_MESSAGE = "The live browser was turned off by an admin"
CLI_OFF_MESSAGE = "CLI agents are turned off by an admin (Settings → Features)"


class FeatureSwitches:
    def __init__(
        self,
        settings: Any,
        sessionmaker: async_sessionmaker | None,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        # Tests stub the health probes with an httpx.MockTransport.
        self.http_transport = http_transport
        self._switches: dict[str, bool] = {}
        # Bumped by ``set``: a refresh that read the table before a change must not undo it.
        self._version = 0

    # ------------------------------------------------------------------ state
    def installed(self, key: str) -> bool:
        s = self.settings
        if key == "browser":
            return bool(getattr(s, "browser_url", None))
        if key == "cli_agents":
            local = bool(getattr(s, "runner_local", False)) and not getattr(s, "is_production", False)
            return bool(getattr(s, "cli_agents_enabled", False)) and (
                bool(getattr(s, "runner_url", None)) or local
            )
        return False

    def switch(self, key: str) -> bool:
        """The admin's stored switch; on when no row exists."""
        return self._switches.get(key, True)

    def is_on(self, key: str) -> bool:
        return self.installed(key) and self.switch(key)

    async def refresh(self) -> None:
        """Reload the cache from the database; on failure the last known switches are kept."""
        if self.sessionmaker is None:
            return
        version = self._version
        try:
            async with self.sessionmaker() as s:
                rows = (await s.execute(select(FeatureSwitch.key, FeatureSwitch.enabled))).all()
        except Exception as exc:
            log.warning("features.refresh_failed", error=str(exc)[:200])
            return
        if version == self._version:
            self._switches = {key: bool(enabled) for key, enabled in rows}

    async def set(
        self, key: str, enabled: bool, actor_id: uuid.UUID | None, *, db: AsyncSession | None = None
    ) -> bool:
        """Store the switch (upsert) and update the cache. Returns the previous stored value.

        With ``db`` (an API request's session) the row is written and committed there; otherwise
        in a short session of its own. The cache changes only once the row is committed.
        """
        if key not in FEATURE_KEYS:
            raise ValueError(f"unknown feature: {key}")
        if db is not None:
            previous = await self._write(db, key, enabled, actor_id)
            await db.commit()
        elif self.sessionmaker is not None:
            async with self.sessionmaker() as s:
                previous = await self._write(s, key, enabled, actor_id)
                await s.commit()
        else:
            previous = self.switch(key)
        self._version += 1
        self._switches[key] = enabled
        return previous

    @staticmethod
    async def _write(db: AsyncSession, key: str, enabled: bool, actor_id: uuid.UUID | None) -> bool:
        row = await db.get(FeatureSwitch, key)
        if row is None:
            db.add(FeatureSwitch(key=key, enabled=enabled, updated_by_id=actor_id, updated_at=utcnow()))
            return True
        previous = bool(row.enabled)
        row.enabled, row.updated_by_id, row.updated_at = enabled, actor_id, utcnow()
        return previous

    # ------------------------------------------------------------------ health
    def _service_url(self, key: str) -> str | None:
        if key == "browser":
            return getattr(self.settings, "browser_url", None) or None
        if key == "cli_agents":
            return getattr(self.settings, "runner_url", None) or None
        return None

    def _client(self, url: str, timeout_s: float) -> tuple[httpx.AsyncClient, str]:
        """A client and the health URL for ``http(s)://host:port`` or ``unix:/path/to/socket``."""
        if url.startswith("unix:"):
            transport = self.http_transport or httpx.AsyncHTTPTransport(uds=url[len("unix:") :])
            return httpx.AsyncClient(transport=transport, timeout=timeout_s), "http://localhost/health"
        return (
            httpx.AsyncClient(transport=self.http_transport, timeout=timeout_s),
            f"{url.rstrip('/')}/health",
        )

    async def probe(self, key: str) -> FeatureStatus:
        """``GET /health`` on the feature's service, bounded by ``health_check_timeout_s``."""
        if not self.installed(key):
            return "not_installed"
        url = self._service_url(key)
        if url is None:
            # Installed without a service URL: the development runner runs the CLIs in-process.
            return "reachable"
        timeout_s = float(getattr(self.settings, "health_check_timeout_s", 2.0))

        async def check() -> bool:
            client, health = self._client(url, timeout_s)
            async with client:
                resp = await client.get(health)
            return resp.status_code == 200

        try:
            ok = await asyncio.wait_for(check(), timeout_s)
        except Exception:
            ok = False
        return "reachable" if ok else "unreachable"
