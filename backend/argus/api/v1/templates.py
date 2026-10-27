"""Run templates: saved run configurations whose topic/description carry ``{{variables}}``."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, write_scope
from argus.api.schemas import CouncilMode, OutputFormat, RunIn, RunOut, _valid_guardrails
from argus.core.errors import ArgusError, Conflict, NotFound
from argus.db.models import RunTemplate
from argus.services.councils import get_council
from argus.services.runs import create_and_start_run

router = APIRouter(prefix="/templates", tags=["templates"], dependencies=[write_scope("runs:write")])

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")


class InvalidVariables(ArgusError):
    status_code = 422
    code = "invalid_variables"


def variables_of(*texts: str) -> list[str]:
    """Distinct variable names in order of first appearance."""
    seen: dict[str, None] = {}
    for text in texts:
        for m in VAR_RE.finditer(text or ""):
            seen.setdefault(m.group(1), None)
    return list(seen)


def render(text: str, values: dict[str, str]) -> str:
    return VAR_RE.sub(lambda m: values[m.group(1)], text or "")


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=500)
    topic: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=20_000)
    mode: CouncilMode = "general"
    council_id: uuid.UUID | None = None
    size: int | None = Field(default=None, ge=1, le=64)
    provider: str = Field(default="mock", max_length=64)
    model: str = Field(default="mock-1", max_length=120)
    outputs: list[OutputFormat] = Field(default_factory=lambda: ["md"], max_length=7)
    guardrails: dict[str, Any] = Field(default_factory=dict)

    _guardrails = field_validator("guardrails")(_valid_guardrails)


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=500)
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    mode: CouncilMode | None = None
    council_id: uuid.UUID | None = None
    size: int | None = Field(default=None, ge=1, le=64)
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=120)
    outputs: list[OutputFormat] | None = Field(default=None, max_length=7)
    guardrails: dict[str, Any] | None = None

    _guardrails = field_validator("guardrails")(_valid_guardrails)


class TemplateRunIn(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict, max_length=50)


def _out(t: RunTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "summary": t.summary,
        "topic": t.topic,
        "description": t.description,
        "mode": t.mode,
        "council_id": str(t.council_id) if t.council_id else None,
        "size": t.size,
        "provider": t.provider,
        "model": t.model,
        "outputs": list(t.outputs or []),
        "guardrails": t.guardrails or {},
        "variables": variables_of(t.topic, t.description),
        "use_count": t.use_count,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


async def _own(db, owner_id: uuid.UUID, template_id: uuid.UUID) -> RunTemplate:
    t = await db.get(RunTemplate, template_id)
    if t is None or t.owner_id != owner_id:
        raise NotFound("Template not found")
    return t


async def _name_free(db, owner_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None) -> None:
    q = select(RunTemplate.id).where(RunTemplate.owner_id == owner_id, RunTemplate.name == name)
    if exclude is not None:
        q = q.where(RunTemplate.id != exclude)
    if (await db.execute(q)).first():
        raise Conflict(f"A template named {name!r} already exists")


@router.get("")
async def list_templates(principal: CurrentPrincipal, db: DbDep) -> dict:
    rows = await db.execute(
        select(RunTemplate).where(RunTemplate.owner_id == principal.id).order_by(RunTemplate.name)
    )
    return {"templates": [_out(t) for t in rows.scalars()]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_template(data: TemplateIn, principal: CurrentPrincipal, db: DbDep) -> dict:
    name = data.name.strip()
    await _name_free(db, principal.id, name)
    if data.council_id:
        await get_council(db, principal.id, data.council_id)
    t = RunTemplate(owner_id=principal.id, **{**data.model_dump(), "name": name})
    db.add(t)
    await db.flush()
    await db.refresh(t)
    return _out(t)


@router.get("/{template_id}")
async def get_template(template_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> dict:
    return _out(await _own(db, principal.id, template_id))


@router.patch("/{template_id}")
async def update_template(
    template_id: uuid.UUID, data: TemplatePatch, principal: CurrentPrincipal, db: DbDep
) -> dict:
    t = await _own(db, principal.id, template_id)
    values = data.model_dump(exclude_unset=True)
    for key in ("name", "topic", "mode", "provider", "model", "outputs", "guardrails"):
        if key in values and values[key] is None:
            values.pop(key)  # explicit null on a required field means "unchanged"
    if "name" in values:
        values["name"] = values["name"].strip()
        await _name_free(db, principal.id, values["name"], exclude=t.id)
    if values.get("council_id"):
        await get_council(db, principal.id, values["council_id"])
    for k, v in values.items():
        setattr(t, k, v)
    await db.flush()
    await db.refresh(t)
    return _out(t)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    await db.delete(await _own(db, principal.id, template_id))


@router.post("/{template_id}/run", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def run_template(
    template_id: uuid.UUID, data: TemplateRunIn, principal: CurrentPrincipal, db: DbDep, request: Request
):
    t = await _own(db, principal.id, template_id)
    needed = variables_of(t.topic, t.description)
    values = {k: v.strip() for k, v in data.variables.items()}
    missing = [v for v in needed if not values.get(v)]
    if missing:
        raise InvalidVariables(
            f"Missing template variables: {', '.join(missing)}",
            details={"missing": missing, "required": needed},
        )
    try:
        run_in = RunIn(
            topic=render(t.topic, values),
            description=render(t.description, values),
            mode=t.mode,  # type: ignore[arg-type]
            council_id=t.council_id,
            size=t.size,
            provider=t.provider,
            model=t.model,
            outputs=list(t.outputs or ["md"]),
            guardrails=t.guardrails or {},
        )
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()[:5]
        ]
        raise InvalidVariables(
            "The filled-in template is not a valid run", details={"errors": problems}
        ) from None
    t.use_count = (t.use_count or 0) + 1
    t.last_used_at = datetime.now(UTC)
    run = await create_and_start_run(db, request.app.state.runs, principal.id, run_in)
    return RunOut.model_validate(run)
