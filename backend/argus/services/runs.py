"""Run creation shared by the REST API and the MCP / A2A / ACP protocol layers."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections import Counter
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.agents.roles import ROLES, roster_for
from argus.api.schemas import RunIn
from argus.core.errors import ArgusError, NotFound
from argus.core.logging import get_logger
from argus.core.metrics import PROTOCOL_BLOCKING_WAITS, PROTOCOL_WAITS_SHED
from argus.db.models import Run
from argus.runtime.events import TERMINAL_STATUSES
from argus.runtime.guardrails import Guardrails
from argus.runtime.manager import RunManager
from argus.services.councils import get_council

log = get_logger(__name__)

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
    # refresh() opened a transaction; end it so the pooled connection goes back to the pool. Callers
    # (blocking MCP/A2A/ACP calls) may now wait minutes for the run without pinning a connection.
    await db.commit()
    return run


async def wait_terminal(
    sessionmaker: async_sessionmaker,
    run_id: uuid.UUID,
    timeout_s: float,
    *,
    manager: RunManager | None = None,
    poll_s: float = 1.0,
) -> Run:
    """Wait until the run is terminal or the timeout passes; returns the latest row.

    A run live in this process is awaited through its task (no database traffic while it runs).
    Otherwise (another replica, or a run that is not live) the row is polled every ``poll_s``.
    Each check uses its own short session, so no connection is held between checks.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_s, 0.0)
    while True:
        async with sessionmaker() as s:
            run = await s.get(Run, run_id)
        if run is None:
            raise NotFound("Run not found")
        remaining = deadline - loop.time()
        if run.status in TERMINAL_STATUSES or remaining <= 0:
            return run
        live = manager.live.get(run_id) if manager is not None else None
        task = getattr(live, "task", None)
        if task is not None and not task.done():
            # Wakes when the run task ends; never cancels it.
            await asyncio.wait({task}, timeout=remaining)
        else:
            await asyncio.sleep(min(poll_s, remaining))


class BlockingWaits:
    """Bounds how many inbound protocol calls may wait for a run at once in this process."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0

    def try_acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        PROTOCOL_BLOCKING_WAITS.inc()
        return True

    def release(self) -> None:
        self.active -= 1
        PROTOCOL_BLOCKING_WAITS.dec()


def wait_seconds(value: Any, default: float) -> float:
    """Parse a client-supplied wait (seconds); invalid values mean ``default``."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return seconds if math.isfinite(seconds) else default


async def wait_for_run(state: Any, run: Run, timeout_s: float, *, protocol: str) -> Run:
    """Blocking-call wait for ``run`` (MCP ``wait_seconds``, A2A ``blocking``, ACP ``sync``).

    The wait is capped by ``ARGUS_PROTOCOL_MAX_WAIT_S``. When ``ARGUS_PROTOCOL_MAX_BLOCKING_WAITS``
    callers are already waiting, the run's current state is returned at once (the client polls).
    The caller must not hold a DB transaction (``create_and_start_run`` commits before returning).
    """
    timeout = min(max(timeout_s, 0.0), state.settings.protocol_max_wait_s)
    if timeout <= 0:
        return run
    waits: BlockingWaits = state.blocking_waits
    if not waits.try_acquire():
        PROTOCOL_WAITS_SHED.labels(protocol).inc()
        log.warning("protocol.blocking_wait_shed", protocol=protocol, run_id=str(run.id), limit=waits.limit)
        return run
    try:
        return await wait_terminal(state.db.sessionmaker, run.id, timeout, manager=state.runs)
    finally:
        waits.release()
