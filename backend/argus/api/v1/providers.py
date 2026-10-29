from __future__ import annotations

import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, SettingsDep, write_scope
from argus.api.schemas import CompleteIn, ProviderConfigIn, ProviderConfigOut, ProviderConfigPatch
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, Conflict, NotFound
from argus.db.models import ProviderConfig
from argus.providers.base import ProviderError
from argus.providers.catalog import KINDS
from argus.providers.cli.provider import CLI_PROVIDERS
from argus.providers.health import HealthResult, HealthStatus, failure, key_hint
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import LLMRouter, Route, persist_health, validate_base_url

router = APIRouter(tags=["providers"], dependencies=[write_scope("providers:write")])


class UpstreamError(ArgusError):
    status_code = 502
    code = "upstream_error"


class ProviderOut(ProviderConfigOut):
    """Provider config as returned by the API: never the key, only a masked hint plus health."""

    key_hint: str | None = None
    health_status: HealthStatus = "unknown"
    health_detail: str | None = None
    health_checked_at: datetime | None = None
    health_latency_ms: int | None = None


class ProviderTestIn(BaseModel):
    # Optional model for a completion probe; without it the probe lists models first.
    model: str | None = Field(default=None, max_length=120)


class ProviderTestOut(BaseModel):
    provider: str
    ok: bool
    status: HealthStatus
    detail: str | None = None
    latency_ms: int | None = None
    checked_at: datetime
    probe: str  # "list_models" | "completion"
    model: str | None = None
    model_count: int | None = None
    persisted: bool
    config: ProviderOut | None = None


def _router(request: Request) -> LLMRouter:
    return request.app.state.llm


def _out(cfg: ProviderConfig) -> ProviderOut:
    out = ProviderOut.model_validate(cfg)
    out.has_api_key = bool(cfg.api_key_enc)
    if not out.has_api_key:
        out.key_hint = None
    return out


def _set_key(cfg: ProviderConfig, key: str | None, settings) -> None:  # type: ignore[no-untyped-def]
    cfg.api_key_enc = SecretBox(settings).encrypt(key) if key else None
    cfg.key_hint = key_hint(key)


async def _get(db, owner_id: uuid.UUID, provider_id: uuid.UUID) -> ProviderConfig:
    cfg = await db.get(ProviderConfig, provider_id)
    if cfg is None or cfg.owner_id != owner_id:
        raise NotFound("Provider not found")
    return cfg


def _visible_kinds(settings) -> dict:  # type: ignore[no-untyped-def]
    """Provider kinds users may pick; ``mock`` only where the mock provider is enabled."""
    return {k: v for k, v in KINDS.items() if k != "mock" or settings.mock_provider_enabled}


@router.get("/providers/kinds")
async def kinds(_: CurrentPrincipal, request: Request, settings: SettingsDep) -> dict:
    return {
        "kinds": [
            {
                "key": k.key,
                "label": k.label,
                "protocol": k.protocol,
                "default_base_url": k.default_base_url,
                "requires_api_key": k.requires_api_key,
                "self_hosted": k.self_hosted,
                "suggested_models": list(k.suggested_models),
            }
            for k in _visible_kinds(settings).values()
        ],
        "system_providers": _router(request).available_system_providers(),
        # CLI agents enabled on this instance ("claude-cli", ...): account-authenticated seats.
        "cli_providers": cli.available_names() if (cli := getattr(_router(request), "cli", None)) else [],
    }


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(principal: CurrentPrincipal, db: DbDep, settings: SettingsDep):
    rows = await db.execute(
        select(ProviderConfig).where(ProviderConfig.owner_id == principal.id).order_by(ProviderConfig.name)
    )
    configs = list(rows.scalars())
    # Backfill hints for keys stored before hints existed (cheap local decrypt, once).
    box = None
    for cfg in configs:
        if cfg.api_key_enc and not cfg.key_hint:
            box = box or SecretBox(settings)
            try:
                cfg.key_hint = key_hint(box.decrypt(cfg.api_key_enc))
            except Exception:  # noqa: S112 - undecryptable keys just stay without a hint
                continue
    if box is not None:
        await db.flush()
    return [_out(c) for c in configs]


@router.post("/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    data: ProviderConfigIn, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
):
    principal.require_scope("providers:write")
    if data.kind not in _visible_kinds(settings):
        raise ArgusError(f"Unknown provider kind '{data.kind}'")
    spec = KINDS[data.kind]
    if spec.protocol == "openai" and not (data.base_url or spec.default_base_url):
        raise ArgusError("base_url is required for this provider kind")
    exists = await db.execute(
        select(ProviderConfig.id).where(
            ProviderConfig.owner_id == principal.id, ProviderConfig.name == data.name
        )
    )
    if exists.first() or data.name in CLI_PROVIDERS:
        raise Conflict(f"Provider '{data.name}' already exists")
    cfg = ProviderConfig(
        owner_id=principal.id,
        name=data.name,
        kind=data.kind,
        base_url=validate_base_url(data.base_url, settings.provider_allow_private_urls)
        if data.base_url
        else None,
        default_model=data.default_model,
        enabled=data.enabled,
        options=data.options,
    )
    _set_key(cfg, data.api_key, settings)
    db.add(cfg)
    await db.flush()
    return _out(cfg)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: uuid.UUID,
    data: ProviderConfigPatch,
    principal: CurrentPrincipal,
    db: DbDep,
    settings: SettingsDep,
):
    principal.require_scope("providers:write")
    cfg = await _get(db, principal.id, provider_id)
    values = data.model_dump(exclude_unset=True)
    if "api_key" in values or values.get("base_url", cfg.base_url) != cfg.base_url:
        # A new credential or endpoint invalidates the last known health.
        cfg.health_status, cfg.health_detail = "unknown", None
        cfg.health_checked_at, cfg.health_latency_ms = None, None
    if "api_key" in values:
        _set_key(cfg, values.pop("api_key"), settings)
    if values.get("base_url"):
        values["base_url"] = validate_base_url(values["base_url"], settings.provider_allow_private_urls)
    for k, v in values.items():
        setattr(cfg, k, v)
    await db.flush()
    return _out(cfg)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    principal.require_scope("providers:write")
    await db.delete(await _get(db, principal.id, provider_id))


@router.get("/providers/{name}/models")
async def list_models(name: str, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    llm = _router(request)
    provider = await llm.resolve(db, principal.id, name)

    async def fetch() -> list[str]:
        return await provider.list_models()

    try:
        models = await llm.cache.get_or_set(llm.cache.key("models", str(principal.id), name), fetch, ttl=600)
    except ProviderError as exc:
        llm.note_health(provider, failure(exc))
        raise UpstreamError(str(exc)) from exc
    return {"provider": name, "models": models or []}


async def _probe(provider, model: str | None) -> tuple[HealthResult, str, int | None]:  # type: ignore[no-untyped-def]
    """With a model: a tiny completion. Without: list models (spends no tokens)."""
    start = time.perf_counter()
    probe = "completion" if model else "list_models"
    try:
        if model:
            await provider.complete(
                CompletionRequest(model=model, messages=[Message.user("ping")], max_tokens=16)
            )
            models = None
        else:
            models = await provider.list_models()
    except Exception as exc:  # every failure is reported as a health status
        return failure(exc, int((time.perf_counter() - start) * 1000)), probe, None
    latency = int((time.perf_counter() - start) * 1000)
    if models is None:
        return (
            HealthResult("ok", detail=f"Completion with {model} succeeded", latency_ms=latency),
            probe,
            None,
        )
    detail = f"{len(models)} models available" if models else None
    return HealthResult("ok", detail=detail, latency_ms=latency), probe, len(models)


@router.post("/providers/{name}/test", response_model=ProviderTestOut)
async def test_provider(
    name: str,
    principal: CurrentPrincipal,
    db: DbDep,
    request: Request,
    data: ProviderTestIn | None = None,
) -> ProviderTestOut:
    """Probe a provider's connection now; persists the result on the user's config."""
    principal.require_scope("providers:write")
    llm = _router(request)
    cfg = (
        await db.execute(
            select(ProviderConfig).where(ProviderConfig.owner_id == principal.id, ProviderConfig.name == name)
        )
    ).scalar_one_or_none()
    if cfg is not None:
        # Tests run even for disabled configs so a connection can be verified before enabling it.
        provider = llm.provider_for_config(cfg)
        model = data.model if data else None
    else:
        provider = await llm.resolve(None, None, name)  # system provider, or NotFound
        model = data.model if data else None
    result, probe, count = await _probe(provider, model)
    if result.ok and probe == "list_models" and not count:
        # Listing unsupported: confirm with a tiny completion against the default model.
        fallback = cfg.default_model if cfg is not None else None
        if fallback:
            result, probe, count = await _probe(provider, fallback)
            model = fallback
        else:
            result.detail = "Reachable (provider does not list models; set a default model for a full check)"
    if cfg is not None:
        await persist_health(db, cfg.id, result)
        await db.refresh(cfg)
        llm.remember_health(cfg.id, result)
    return ProviderTestOut(
        provider=name,
        ok=result.ok,
        status=result.status,
        detail=result.detail,
        latency_ms=result.latency_ms,
        checked_at=result.checked_at,
        probe=probe,
        model=model if probe == "completion" else None,
        model_count=count,
        persisted=cfg is not None,
        config=_out(cfg) if cfg is not None else None,
    )


@router.post("/llm/complete")
async def complete(data: CompleteIn, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    """Single completion through the router; used by the UI playground and for provider tests."""
    req = CompletionRequest(
        model=data.model,
        messages=[Message(role=m.role, content=m.content) for m in data.messages],
        max_tokens=data.max_tokens,
        temperature=data.temperature,
        effort=data.effort,
        metadata={"cache": data.cache} if data.cache is not None else {},
    )
    fallbacks = [Route(f["provider"], f["model"]) for f in data.fallbacks if "provider" in f and "model" in f]
    try:
        resp = await _router(request).complete(
            req, Route(data.provider, data.model), db=db, owner_id=principal.id, fallbacks=fallbacks
        )
    except ProviderError as exc:
        raise UpstreamError(str(exc)) from exc
    return resp.model_dump(mode="json", exclude={"raw"})
