"""LLM router: resolves providers per user, applies retries, fallbacks and caching.

Resolution order for a provider name:
1. the user's own ``ProviderConfig`` with that name (encrypted key),
2. a system provider configured from the environment
   (``ARGUS_<KIND>_API_KEY`` / ``<KIND>_API_KEY`` and ``ARGUS_<KIND>_BASE_URL``),
3. ``mock`` - always available, offline and deterministic.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.cache.store import Cache
from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, NotFound
from argus.core.logging import get_logger
from argus.db.models import ProviderConfig
from argus.providers.base import Provider, ProviderError, with_retries
from argus.providers.catalog import KINDS, build_provider
from argus.providers.mock import MockProvider
from argus.providers.pricing import register_pricing
from argus.providers.types import CompletionRequest, CompletionResponse, StreamEvent, Usage

log = get_logger(__name__)

_ENV_ALIASES = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}


class InvalidProviderURL(ArgusError):
    code = "invalid_provider_url"


def validate_base_url(url: str, allow_private: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidProviderURL("base_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise InvalidProviderURL("base_url must not embed credentials")
    if not allow_private:
        try:
            infos = socket.getaddrinfo(parsed.hostname, None)
        except socket.gaierror as exc:
            raise InvalidProviderURL("base_url host does not resolve") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise InvalidProviderURL("base_url resolves to a private address, which is disabled")
    return url.rstrip("/")


def system_providers_from_env(env: dict[str, str] | None = None) -> dict[str, Provider]:
    env = env if env is not None else dict(os.environ)
    found: dict[str, Provider] = {"mock": MockProvider()}
    for key, spec in KINDS.items():
        if key == "mock":
            continue
        upper = key.upper()
        names = (f"ARGUS_{upper}_API_KEY", f"{upper}_API_KEY", *_ENV_ALIASES.get(key, ()))
        api_key = next((env[n] for n in names if env.get(n)), None)
        base_url = env.get(f"ARGUS_{upper}_BASE_URL")
        if (spec.requires_api_key and api_key) or (not spec.requires_api_key and base_url):
            found[key] = build_provider(key, api_key=api_key, base_url=base_url)
    return found


@dataclass
class Route:
    provider: str
    model: str


@dataclass
class UsageLedger:
    """Accumulates spend; the council runtime uses this to enforce budgets."""

    total: Usage = field(default_factory=Usage)
    calls: int = 0
    by_model: dict[str, Usage] = field(default_factory=dict)

    def record(self, response: CompletionResponse) -> None:
        self.calls += 1
        self.total = self.total + response.usage
        key = f"{response.provider}/{response.model}"
        self.by_model[key] = self.by_model.get(key, Usage()) + response.usage


class LLMRouter:
    def __init__(self, settings: Settings, cache: Cache, system: dict[str, Provider] | None = None) -> None:
        self.settings = settings
        self.cache = cache
        self.system = system if system is not None else system_providers_from_env()
        self.box = SecretBox(settings)
        self._user_providers: dict[tuple[uuid.UUID, str, str], Provider] = {}
        for model, p in settings.model_pricing.items():
            register_pricing(
                model,
                p.get("input", 0.0),
                p.get("output", 0.0),
                cache_read=p.get("cache_read"),
                cache_write=p.get("cache_write"),
            )

    # ------------------------------------------------------------ resolution
    async def resolve(self, db: AsyncSession | None, owner_id: uuid.UUID | None, name: str) -> Provider:
        if db is not None and owner_id is not None:
            cfg = (
                await db.execute(
                    select(ProviderConfig).where(
                        ProviderConfig.owner_id == owner_id, ProviderConfig.name == name
                    )
                )
            ).scalar_one_or_none()
            if cfg is not None:
                if not cfg.enabled:
                    raise ProviderError(f"Provider '{name}' is disabled", provider=name)
                return self._from_config(cfg)
        if name in self.system:
            return self.system[name]
        raise NotFound(f"Provider '{name}' is not configured")

    def _from_config(self, cfg: ProviderConfig) -> Provider:
        # Cache constructed clients; the version stamp invalidates on edits.
        stamp = cfg.updated_at.isoformat() if cfg.updated_at else ""
        key = (cfg.owner_id, cfg.name, stamp)
        if key not in self._user_providers:
            for stale in [k for k in self._user_providers if k[:2] == key[:2]]:
                self._user_providers.pop(stale)
            api_key = self.box.decrypt(cfg.api_key_enc) if cfg.api_key_enc else None
            self._user_providers[key] = build_provider(
                cfg.kind, name=cfg.name, api_key=api_key, base_url=cfg.base_url, options=cfg.options
            )
        return self._user_providers[key]

    def available_system_providers(self) -> list[str]:
        return sorted(self.system)

    # ------------------------------------------------------------ calls
    def _cacheable(self, req: CompletionRequest) -> bool:
        flag = req.metadata.get("cache")
        return bool(flag) if flag is not None else req.temperature == 0

    def _cache_key(self, owner_id: uuid.UUID | None, provider: str, req: CompletionRequest) -> str:
        body = req.model_dump(exclude={"metadata"})
        return self.cache.key("llm", self.cache.fingerprint([str(owner_id), provider, body]))

    async def complete(
        self,
        req: CompletionRequest,
        route: Route,
        *,
        db: AsyncSession | None = None,
        owner_id: uuid.UUID | None = None,
        fallbacks: list[Route] | None = None,
        ledger: UsageLedger | None = None,
    ) -> CompletionResponse:
        """Try ``route`` then each fallback; non-retryable 4xx errors still fall through."""
        last_error: Exception | None = None
        for r in [route, *(fallbacks or [])]:
            request = req.model_copy(update={"model": r.model})
            cache_key = self._cache_key(owner_id, r.provider, request) if self._cacheable(request) else None
            if cache_key:
                hit = await self.cache.get(cache_key)
                if hit is not None:
                    resp = CompletionResponse.model_validate(hit)
                    resp.cached = True
                    resp.usage = Usage()  # cached responses cost nothing
                    if ledger:
                        ledger.calls += 1
                    return resp
            try:
                provider = await self.resolve(db, owner_id, r.provider)
                resp = await with_retries(
                    lambda p=provider, q=request: p.complete(q), attempts=self.settings.llm_max_retries
                )
            except (ProviderError, NotFound) as exc:
                last_error = exc
                log.warning("llm.route_failed", provider=r.provider, model=r.model, error=str(exc)[:200])
                continue
            if ledger:
                ledger.record(resp)
            if cache_key and resp.finish_reason in ("stop", "tool_calls"):
                await self.cache.set(cache_key, resp.model_dump(mode="json"), ttl=self.settings.llm_cache_ttl)
            return resp
        assert last_error is not None
        raise last_error

    async def stream(
        self,
        req: CompletionRequest,
        route: Route,
        *,
        db: AsyncSession | None = None,
        owner_id: uuid.UUID | None = None,
        ledger: UsageLedger | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        provider = await self.resolve(db, owner_id, route.provider)
        request = req.model_copy(update={"model": route.model})
        async for event in provider.stream(request):
            if event.type == "done" and event.response and ledger:
                ledger.record(event.response)
            if on_event:
                on_event(event)
            yield event

    async def aclose(self) -> None:
        for p in [*self.system.values(), *self._user_providers.values()]:
            try:
                await p.aclose()
            except Exception:  # noqa: S110
                pass
