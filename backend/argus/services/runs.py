"""Run creation shared by the REST API and the MCP / A2A / ACP protocol layers."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.agents.roles import ROLES, roster_for
from argus.api.schemas import RunIn
from argus.core.errors import ArgusError
from argus.db.models import Run
from argus.runtime.events import TERMINAL_STATUSES
from argus.runtime.guardrails import Guardrails
from argus.runtime.manager import RunManager
from argus.services.councils import get_council

DEFAULT_SIZES = {"chat": 2, "coding": 6, "research": 6}


def ephemeral_roster(mode: str, size: int, provider: str, model: str) -> list[dict]:
    taken: Counter[str] = Counter()
    members = []
    for i, role in enumerate(roster_for(mode, size)):
        spec = ROLES[role]
        taken[role] += 1
        members.append(
            {
                "id": str(uuid.uuid4()),
                "name": spec.title if taken[role] == 1 else f"{spec.title} {taken[role]}",
                "role": role,
                "provider": provider,
                "model": model,
                "system_prompt": spec.system_prompt,
                "temperature": None,
                "position": i,
                "enabled": True,
                "config": {"tools": list(spec.default_tools), "overseer": spec.overseer},
            }
        )
    return members


def mode_defaults(mode: str, manager: RunManager) -> dict:
    """Base guardrail layer per mode (council and run settings override it)."""
    from argus.sandbox.client import RemoteSandbox

    if mode == "coding" and isinstance(getattr(manager, "sandbox", None), RemoteSandbox):
        # Commands run in an isolated, network-less sandbox: no per-command approval needed.
        return {"approval_required": ["package_install", "remote_agent", "network_write"]}
    return {}


async def create_and_start_run(
    db: AsyncSession,
    manager: RunManager,
    owner_id: uuid.UUID,
    data: RunIn,
    *,
    parent_run_id: uuid.UUID | None = None,
) -> Run:
    council_guardrails: dict = {}
    if data.council_id:
        council = await get_council(db, owner_id, data.council_id)
        members = [
            {
                "id": str(m.id),
                "name": m.name,
                "role": m.role,
                "provider": m.provider,
                "model": m.model,
                "system_prompt": m.system_prompt,
                "temperature": m.temperature,
                "position": m.position,
                "enabled": m.enabled,
                "config": m.config,
            }
            for m in council.members
        ]
        council_guardrails = council.guardrails or {}
        snapshot = {"council_id": str(council.id), "name": council.name, "members": members}
        mode = data.mode if data.mode != "general" else council.mode
    else:
        size = data.size or DEFAULT_SIZES.get(data.mode, 5)
        snapshot = {
            "council_id": None,
            "name": "Ad-hoc council",
            "members": ephemeral_roster(data.mode, size, data.provider, data.model),
        }
        mode = data.mode
    try:
        rails = Guardrails.resolve(mode_defaults(mode, manager), council_guardrails, data.guardrails)
    except ValidationError as exc:
        raise ArgusError(
            "Invalid guardrails", details={"errors": exc.errors(include_url=False)[:10]}
        ) from exc
    run = Run(
        owner_id=owner_id,
        council_id=data.council_id,
        parent_run_id=parent_run_id,
        topic=data.topic,
        description=data.description,
        mode=mode,
        outputs=list(data.outputs),
        guardrails=rails.model_dump(),
        council_snapshot=snapshot,
    )
    db.add(run)
    await db.commit()
    await manager.start(run.id)
    await db.refresh(run)
    return run


async def wait_terminal(sessionmaker: async_sessionmaker, run_id: uuid.UUID, timeout_s: float) -> Run:
    """Poll until the run reaches a terminal status or the timeout passes; returns the latest row."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        async with sessionmaker() as s:
            run = await s.get(Run, run_id)
        assert run is not None
        if run.status in TERMINAL_STATUSES or asyncio.get_running_loop().time() >= deadline:
            return run
        await asyncio.sleep(0.1)
