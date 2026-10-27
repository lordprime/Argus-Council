"""LLM router: resolves providers per user, applies retries, fallbacks and caching.

Resolution order for a provider name:
1. the user's own ``ProviderConfig`` with that name (encrypted key),
2. a system provider configured from the environment
   (``ARGUS_<KIND>_API_KEY`` / ``<KIND>_API_KEY`` and ``ARGUS_<KIND>_BASE_URL``),
3. ``mock`` - always available, offline and deterministic.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.cache.store import Cache
from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, NotFound
from argus.core.logging import get_logger
from argus.core.netguard import ip_blocked
from argus.db.models import ProviderConfig
from argus.providers.base import Provider, ProviderError, retry_delay, with_retries
from argus.providers.catalog import KINDS, build_provider
from argus.providers.health import HealthResult, failure
from argus.providers.mock import MockProvider
from argus.providers.pricing import register_pricing
from argus.providers.types import CompletionRequest, CompletionResponse, StreamEvent, Usage

log = get_logger(__name__)

_ENV_ALIASES = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}


class InvalidProviderURL(ArgusError):
    code = "invalid_provider_url"


def validate_base_url(url: str, allow_private: bool, *, require_dns: bool = True) -> str:
    """Check a user-supplied base URL. Link-local (cloud metadata) targets are always refused.

    With ``require_dns=False`` an unresolvable host is accepted: callers that connect through
    :func:`argus.core.netguard.guarded_client` re-check every address at connect time anyway.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidProviderURL("base_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise InvalidProviderURL("base_url must not embed credentials")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, UnicodeError) as exc:
        if require_dns and not allow_private:
            raise InvalidProviderURL("base_url host does not resolve") from exc
        infos = []
    for info in infos:
        if ip_blocked(info[4][0], allow_private):
            raise InvalidProviderURL(
                "base_url resolves to a link-local or metadata address, which is never allowed"
                if allow_private
                else "base_url resolves to a private address, which is disabled"
            )
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


async def persist_health(session: AsyncSession, config_id: uuid.UUID, result: HealthResult) -> None:
    """Write a health result onto a ProviderConfig without bumping ``updated_at``.

    ``updated_at`` versions the router's client cache, so health writes must not touch it.
    """
    await session.execute(
        update(ProviderConfig)
        .where(ProviderConfig.id == config_id)
        .values(
            health_status=result.status,
            health_detail=result.detail[:300] if result.detail else None,
            health_checked_at=result.checked_at,
            health_latency_ms=result.latency_ms,
            updated_at=ProviderConfig.updated_at,
        )
        .execution_options(synchronize_session=False)
    )


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class LLMRouter:
    # Passive health: re-write an unchanged status at most this often (seconds).
    HEALTH_WRITE_INTERVAL = 60.0
    # First backoff step for retryable provider errors; doubles per attempt, with jitter.
    retry_base_delay = 0.5

    def __init__(
        self,
        settings: Settings,
        cache: Cache,
        system: dict[str, Provider] | None = None,
        sessionmaker: async_sessionmaker | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.cache = cache
        self.system = system if system is not None else system_providers_from_env()
        self.box = SecretBox(settings)
        self._user_providers: dict[tuple[uuid.UUID, str, str], Provider] = {}
        self._health_tasks: set[asyncio.Task[None]] = set()
        self._health_last: dict[uuid.UUID, tuple[str, float]] = {}
        # Failures of background health writes (logged too); tests assert this stays empty.
        self.health_errors: list[BaseException] = []
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
        if db is None and owner_id is not None and self.sessionmaker is not None:
            # Background work (council runs) has no request session; open a short-lived one.
            async with self.sessionmaker() as session:
                return await self.resolve(session, owner_id, name)
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
            provider = build_provider(
                cfg.kind, name=cfg.name, api_key=api_key, base_url=cfg.base_url, options=cfg.options
            )
            provider.config_id = cfg.id
            self._user_providers[key] = provider
        return self._user_providers[key]

    def provider_for_config(self, cfg: ProviderConfig) -> Provider:
        """Build (or reuse) the client for a config regardless of ``enabled`` (connection tests)."""
        return self._from_config(cfg)

    # ------------------------------------------------------------ passive health
    def note_health(self, provider: Provider, result: HealthResult) -> None:
        """Best-effort, non-blocking health update for user-configured providers."""
        config_id = provider.config_id
        if config_id is None or self.sessionmaker is None:
            return
        now = time.monotonic()
        last = self._health_last.get(config_id)
        if last and last[0] == result.status and now - last[1] < self.HEALTH_WRITE_INTERVAL:
            return
        self._health_last[config_id] = (result.status, now)
        task = asyncio.create_task(self._write_health(config_id, result))
        self._health_tasks.add(task)
        task.add_done_callback(self._health_done)

    def remember_health(self, config_id: uuid.UUID, result: HealthResult) -> None:
        """Record a health result persisted elsewhere (e.g. an explicit test) for throttling."""
        self._health_last[config_id] = (result.status, time.monotonic())

    async def _write_health(self, config_id: uuid.UUID, result: HealthResult) -> None:
        assert self.sessionmaker is not None
        async with self.sessionmaker() as session:
            await persist_health(session, config_id, result)
            await session.commit()

    def _health_done(self, task: asyncio.Task[None]) -> None:
        self._health_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.health_errors.append(exc)
            log.warning("llm.health_write_failed", error=f"{type(exc).__name__}: {exc}"[:200])

    async def flush_health(self) -> None:
        """Wait for pending background health writes (shutdown, tests)."""
        while self._health_tasks:
            await asyncio.gather(*list(self._health_tasks), return_exceptions=True)

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
            provider: Provider | None = None
            try:
                provider = await self.resolve(db, owner_id, r.provider)
                start = time.perf_counter()
                try:
                    resp = await with_retries(
                        lambda p=provider, q=request: p.complete(q),
                        attempts=self.settings.llm_max_retries,
                        base_delay=self.retry_base_delay,
                    )
                except ProviderError as exc:
                    self.note_health(provider, failure(exc, _elapsed_ms(start)))
                    raise
                self.note_health(provider, HealthResult("ok", latency_ms=_elapsed_ms(start)))
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
        """Stream one completion.

        A retryable error (429, 5xx, overloaded, connection reset) that happens before the first
        event is retried with the same jittered backoff as ``complete()``. Once an event has been
        handed to the caller the error is raised instead: replaying the stream would duplicate
        output, so the caller decides how to recover.
        """
        provider = await self.resolve(db, owner_id, route.provider)
        request = req.model_copy(update={"model": route.model})
        attempts = max(self.settings.llm_max_retries, 1)
        for attempt in range(1, attempts + 1):
            start = time.perf_counter()
            emitted = False
            try:
                async for event in provider.stream(request):
                    if event.type == "done":
                        self.note_health(provider, HealthResult("ok", latency_ms=_elapsed_ms(start)))
                        if event.response and ledger:
                            ledger.record(event.response)
                    if on_event:
                        on_event(event)
                    emitted = True
                    yield event
                return
            except ProviderError as exc:
                self.note_health(provider, failure(exc, _elapsed_ms(start)))
                if emitted or not exc.retryable or attempt == attempts:
                    raise
                delay = retry_delay(attempt, exc, base_delay=self.retry_base_delay)
                log.warning(
                    "llm.stream_retry", provider=route.provider, attempt=attempt, delay=round(delay, 2)
                )
                await asyncio.sleep(delay)

    async def aclose(self) -> None:
        await self.flush_health()
        for p in [*self.system.values(), *self._user_providers.values()]:
            try:
                await p.aclose()
            except Exception:  # noqa: S110
                pass
