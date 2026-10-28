"""CLI agents: status, per-user CLI API keys and the caller's own CLI sessions.

Every route works on the signed-in user's own data only; there is no admin variant, so an admin
can never use, inspect or reset another user's CLI login or sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field, field_validator

from argus.api.deps import CurrentPrincipal, DbDep, write_scope
from argus.core.errors import NotFound
from argus.db.models import CliSession
from argus.services.cli_agents import CLIAgents, CLIAgentsDisabled

router = APIRouter(tags=["cli-agents"], dependencies=[write_scope("providers:write")])

CliName = Literal["claude", "codex", "gemini"]


def _svc(request: Request) -> CLIAgents:
    return request.app.state.cli_agents


class CliKeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if any(c.isspace() for c in v) or not v.isprintable():
            raise ValueError("api_key must be a single printable token")
        return v


class CliKeyOut(BaseModel):
    cli: str
    has_api_key: bool
    key_hint: str | None = None


class CliSessionOut(BaseModel):
    id: uuid.UUID
    cli: str
    session_id: str
    label: str
    run_id: uuid.UUID | None
    member_id: str | None
    created_at: datetime
    last_used_at: datetime


class CliSessionPatch(BaseModel):
    label: str = Field(max_length=120, pattern=r"^[\w .:\-]*$")


@router.get("/cli-agents")
async def overview(principal: CurrentPrincipal, db: DbDep, request: Request, probe: bool = True) -> dict:
    """Instance switch, policy notice and, per CLI: version, this user's login/key state, login steps."""
    return await _svc(request).overview(db, principal.id, probe=probe)


@router.put("/cli-agents/{cli}/key", response_model=CliKeyOut)
async def set_key(cli: CliName, data: CliKeyIn, principal: CurrentPrincipal, db: DbDep, request: Request):
    svc = _svc(request)
    if not svc.allowed(cli):
        raise CLIAgentsDisabled(f"The {cli} CLI agent is disabled on this instance")
    row = await svc.set_key(db, principal.id, cli, data.api_key)
    return CliKeyOut(cli=cli, has_api_key=True, key_hint=row.key_hint)


@router.delete("/cli-agents/{cli}/key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(cli: CliName, principal: CurrentPrincipal, db: DbDep, request: Request) -> None:
    if not await _svc(request).delete_key(db, principal.id, cli):
        raise NotFound("No API key stored for this CLI")


@router.get("/cli-agents/sessions", response_model=list[CliSessionOut])
async def sessions(principal: CurrentPrincipal, db: DbDep, request: Request, cli: CliName | None = None):
    """Sessions your seats ran in; pin one on a seat (``config.cli_session``) to resume it."""
    rows = await _svc(request).list_sessions(db, principal.id, cli)
    return [CliSessionOut.model_validate(r, from_attributes=True) for r in rows]


@router.patch("/cli-agents/sessions/{session_row_id}", response_model=CliSessionOut)
async def rename_session(
    session_row_id: uuid.UUID, data: CliSessionPatch, principal: CurrentPrincipal, db: DbDep
) -> CliSessionOut:
    row = await db.get(CliSession, session_row_id)
    if row is None or row.owner_id != principal.id:
        raise NotFound("Session not found")
    row.label = data.label.strip()
    await db.flush()
    return CliSessionOut.model_validate(row, from_attributes=True)
