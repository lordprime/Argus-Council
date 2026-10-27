from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, SettingsDep
from argus.api.schemas import CompleteIn, ProviderConfigIn, ProviderConfigOut, ProviderConfigPatch
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError, Conflict, NotFound
from argus.db.models import ProviderConfig
from argus.providers.base import ProviderError
from argus.providers.catalog import KINDS
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import LLMRouter, Route, validate_base_url

router = APIRouter(tags=["providers"])


class UpstreamError(ArgusError):
    status_code = 502
    code = "upstream_error"


def _router(request: Request) -> LLMRouter:
    return request.app.state.llm


def _out(cfg: ProviderConfig) -> ProviderConfigOut:
    out = ProviderConfigOut.model_validate(cfg)
    out.has_api_key = bool(cfg.api_key_enc)
    return out


async def _get(db, owner_id: uuid.UUID, provider_id: uuid.UUID) -> ProviderConfig:
    cfg = await db.get(ProviderConfig, provider_id)
    if cfg is None or cfg.owner_id != owner_id:
        raise NotFound("Provider not found")
    return cfg


@router.get("/providers/kinds")
async def kinds(_: CurrentPrincipal, request: Request) -> dict:
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
            for k in KINDS.values()
        ],
        "system_providers": _router(request).available_system_providers(),
    }


@router.get("/providers", response_model=list[ProviderConfigOut])
async def list_providers(principal: CurrentPrincipal, db: DbDep):
    rows = await db.execute(
        select(ProviderConfig).where(ProviderConfig.owner_id == principal.id).order_by(ProviderConfig.name)
    )
    return [_out(c) for c in rows.scalars()]


@router.post("/providers", response_model=ProviderConfigOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    data: ProviderConfigIn, principal: CurrentPrincipal, db: DbDep, settings: SettingsDep
):
    principal.require_scope("providers:write")
    if data.kind not in KINDS:
        raise ArgusError(f"Unknown provider kind '{data.kind}'")
    spec = KINDS[data.kind]
    if spec.protocol == "openai" and not (data.base_url or spec.default_base_url):
        raise ArgusError("base_url is required for this provider kind")
    exists = await db.execute(
        select(ProviderConfig.id).where(
            ProviderConfig.owner_id == principal.id, ProviderConfig.name == data.name
        )
    )
    if exists.first():
        raise Conflict(f"Provider '{data.name}' already exists")
    cfg = ProviderConfig(
        owner_id=principal.id,
        name=data.name,
        kind=data.kind,
        base_url=validate_base_url(data.base_url, settings.provider_allow_private_urls)
        if data.base_url
        else None,
        api_key_enc=SecretBox(settings).encrypt(data.api_key) if data.api_key else None,
        default_model=data.default_model,
        enabled=data.enabled,
        options=data.options,
    )
    db.add(cfg)
    await db.flush()
    return _out(cfg)


@router.patch("/providers/{provider_id}", response_model=ProviderConfigOut)
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
    if "api_key" in values:
        key = values.pop("api_key")
        cfg.api_key_enc = SecretBox(settings).encrypt(key) if key else None
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
        raise UpstreamError(str(exc)) from exc
    return {"provider": name, "models": models or []}


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
