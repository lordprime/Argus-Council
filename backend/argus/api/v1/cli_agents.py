"""CLI agents: status, sign-in, per-user CLI API keys and the caller's own CLI sessions.

Every route works on the signed-in user's own data only; there is no admin variant, so an admin
can never use, inspect, sign in or reset another user's CLI login or sessions. Sign-in and sign-out
need an interactive session (not an API key), like other account management.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field, field_validator

from argus.api.deps import CacheDep, CurrentPrincipal, DbDep, SessionPrincipal, write_scope
from argus.core.errors import NotFound, RateLimited
from argus.db.models import CliSession
from argus.services.cli_agents import CLIAgents, CLIAgentsDisabled

router = APIRouter(tags=["cli-agents"], dependencies=[write_scope("providers:write")])

CliName = Literal["claude", "codex", "gemini", "copilot", "kiro", "qwen", "opencode", "goose", "vibe"]


def _svc(request: Request) -> CLIAgents:
    return request.app.state.cli_agents


class CliKeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)
    # Harness CLIs (qwen, opencode, goose): which model provider the key is for.
    provider: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_]+$")

    @field_validator("api_key")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if any(c.isspace() for c in v) or not v.isprintable():
            raise ValueError("api_key must be a single printable token")
        return v


class CliLoginStartIn(BaseModel):
    method: str | None = Field(default=None, max_length=32, pattern=r"^[a-z_]+$")


class CliLoginCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=512)

    @field_validator("code")
    @classmethod
    def _one_token(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v) or not v.isprintable():
            raise ValueError("The code must be a single line without spaces")
        return v


class CliKeyOut(BaseModel):
    cli: str
    has_api_key: bool
    key_hint: str | None = None
    provider: str | None = None


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
    row = await svc.set_key(db, principal.id, cli, data.api_key, data.provider)
    return CliKeyOut(cli=cli, has_api_key=True, key_hint=row.key_hint, provider=row.provider)


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


# ---------------------------------------------------------------------------------------- sign-in
LOGIN_STARTS_PER_10_MIN = 6
LOGIN_CODES_PER_10_MIN = 10


@router.get("/cli-agents/{cli}/login")
async def login_state(cli: CliName, principal: SessionPrincipal, request: Request) -> dict:
    """Your sign-in in progress for this CLI (``state: none`` when there is none)."""
    return await _svc(request).login_state(principal.id, cli)


@router.post("/cli-agents/{cli}/login")
async def login_start(
    cli: CliName, data: CliLoginStartIn, principal: SessionPrincipal, request: Request, cache: CacheDep
) -> dict:
    """Start the CLI's own sign-in in your runner home; returns the link (and code) to act on."""
    request.state.audit_detail = {"cli": cli, "action": "login_start", "method": data.method}
    if await cache.hit_rate_limit(f"cli-login:{principal.id}", LOGIN_STARTS_PER_10_MIN, 600):
        raise RateLimited("Too many sign-in attempts; try again in a few minutes")
    state = await _svc(request).login_start(principal.id, cli, data.method)
    request.state.audit_detail["result"] = state.get("state")
    return state


@router.post("/cli-agents/{cli}/login/code")
async def login_code(
    cli: CliName, data: CliLoginCodeIn, principal: SessionPrincipal, request: Request, cache: CacheDep
) -> dict:
    """Pass the authorization code shown by the vendor to the CLI. Never stored or logged."""
    request.state.audit_detail = {"cli": cli, "action": "login_code"}
    if await cache.hit_rate_limit(f"cli-login-code:{principal.id}", LOGIN_CODES_PER_10_MIN, 600):
        raise RateLimited("Too many codes entered; start the sign-in again in a few minutes")
    state = await _svc(request).login_code(principal.id, cli, data.code)
    request.state.audit_detail["result"] = state.get("state")
    return state


@router.delete("/cli-agents/{cli}/login")
async def login_cancel(cli: CliName, principal: SessionPrincipal, request: Request) -> dict:
    request.state.audit_detail = {"cli": cli, "action": "login_cancel"}
    return await _svc(request).login_cancel(principal.id, cli)


@router.post("/cli-agents/{cli}/logout")
async def logout(cli: CliName, principal: SessionPrincipal, request: Request) -> dict:
    """Sign this CLI out of your vendor account (the CLI's own sign-out)."""
    request.state.audit_detail = {"cli": cli, "action": "logout"}
    out = await _svc(request).logout(principal.id, cli)
    status_ = out.get("status") or {}
    return {
        "ok": bool(out.get("ok")),
        "logged_in": status_.get("logged_in"),
        "login_method": status_.get("method"),
    }
