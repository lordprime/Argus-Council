"""Sub-councils: a member delegates a self-contained task to a smaller child council.

The child is a real run linked by ``parent_run_id``. Its guardrails never exceed the parent's
(``Guardrails.clamp_to``), its budget is capped at what the parent has left, its spend is charged
back to the parent, and its live activity is relayed into the parent's stream as ephemeral
``subcouncil.event`` envelopes so the parent's views can draw the child's members and traffic.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from argus.db.models import Run
from argus.providers.types import Usage
from argus.runtime.events import TERMINAL_STATUSES, Envelope
from argus.runtime.guardrails import Guardrails
from argus.tools.base import Tool, ToolCallContext, ToolResult

if TYPE_CHECKING:
    from argus.runtime.manager import RunManager

# Child events worth relaying; deltas and budget ticks would only flood the parent stream.
RELAYED = frozenset(
    {
        "run.plan",
        "step.status",
        "member.status",
        "thought.started",
        "thought.completed",
        "tool.call.started",
        "tool.call.completed",
        "intercept.raised",
        "correction.issued",
        "approval.requested",
        "vote.cast",
        "protocol.call",
        "subcouncil.started",
        "subcouncil.completed",
        "run.status",
    }
)
_KEEP = (
    "status", "step_id", "title", "tool", "tool_call_id", "thought_id", "member_id", "reason", "action",
    "protocol", "peer", "method", "ledger_id", "hash", "duration_ms", "cost_usd", "child_run_id",
)  # fmt: skip


def _brief(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data[k] for k in _KEEP if k in data}
    if isinstance(data.get("steps"), list):
        out["steps"] = len(data["steps"])
    text = data.get("text") or data.get("summary")
    if isinstance(text, str):
        out["text"] = text[:240]
    return out


class SubCouncilTool(Tool):
    name = "subcouncil"
    risk = frozenset({"subcouncil"})
    source = "builtin"
    description = (
        "Delegate a self-contained, well-scoped sub-task to a small child council that plans, works and "
        "reviews on its own, and get its deliverable back. Use it for an independent piece of a larger "
        "task (e.g. a literature survey, one module of a codebase). Write the task so it can be done "
        "without this conversation. It shares your remaining budget and guardrails."
    )

    class Args(BaseModel):
        task: str = Field(min_length=1, max_length=20_000, description="Full, self-contained sub-task")
        mode: Literal["general", "research", "coding", "chat"] = "general"
        size: int = Field(default=3, ge=2, le=6, description="Seats in the child council")

    def __init__(self, manager: RunManager) -> None:
        self.manager = manager

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        from argus.services.runs import ephemeral_roster

        parent = ctx.run
        rails = parent.engine.rails
        # clamp_to lowers the depth allowance by one per level, so the parent's own allowance decides.
        if rails.max_subcouncil_depth <= 0:
            return ToolResult.error(
                f"Sub-council depth limit reached ({rails.max_subcouncil_depth}); do the task yourself."
            )
        child_rails = Guardrails.resolve(rails.model_dump()).clamp_to(rails)
        if rails.budget.usd is not None:
            left = max(rails.budget.usd - parent.engine.usage.cost_usd, 0.0)
            if left <= 0:
                return ToolResult.error("No budget left for a sub-council.")
            child_rails.budget.usd = min(child_rails.budget.usd or left, left)
        member = self.manager.member(parent.run_id, ctx.member_id)
        provider, model = (member.provider, member.model) if member else ("mock", "mock-1")
        roster = ephemeral_roster(args.mode, args.size, provider, model)
        child_id = uuid.uuid4()
        async with parent.sessionmaker() as s:
            s.add(
                Run(
                    id=child_id,
                    owner_id=parent.owner_id,
                    parent_run_id=parent.run_id,
                    topic=args.task[:500],
                    description=args.task if len(args.task) > 500 else "",
                    mode=args.mode,
                    outputs=["md"],
                    guardrails=child_rails.model_dump(),
                    council_snapshot={"council_id": None, "name": "Sub-council", "members": roster},
                )
            )
            await s.commit()

        await parent.sink.emit(
            "subcouncil.started",
            {
                "child_run_id": str(child_id),
                "task": args.task[:500],
                "mode": args.mode,
                "depth": parent.depth + 1,
                "budget_usd": child_rails.budget.usd,
                "tool_call_id": ctx.tool_call_id,
                "members": [
                    {
                        "id": m["id"],
                        "name": m["name"],
                        "role": m["role"],
                        "overseer": bool(m["config"].get("overseer")),
                    }
                    for m in roster
                ],
            },
            actor=_actor(ctx),
            step_id=ctx.step_id,
        )
        queue: asyncio.Queue[Envelope] = asyncio.Queue()
        await self.manager.start(child_id, depth=parent.depth + 1, listeners=[queue.put_nowait])
        try:
            await asyncio.wait_for(self._relay(queue, parent, child_id), child_rails.max_runtime_minutes * 60)
        except TimeoutError:
            await self._stop(child_id)
        except asyncio.CancelledError:
            # The parent was cancelled: never leave an orphaned child council running.
            await asyncio.shield(self._stop(child_id))
            raise
        async with parent.sessionmaker() as s:
            child = await s.get(Run, child_id)
        assert child is not None
        usage = child.usage or {}
        parent.engine.record_usage(
            Usage(
                input_tokens=int(usage.get("tokens_in", 0)),
                output_tokens=int(usage.get("tokens_out", 0)),
                cost_usd=float(usage.get("usd", 0.0)),
            )
        )
        await parent.sink.emit(
            "subcouncil.completed",
            {
                "child_run_id": str(child_id),
                "status": child.status,
                "error": child.error,
                "usage": usage,
                "tool_call_id": ctx.tool_call_id,
            },
            actor=_actor(ctx),
            step_id=ctx.step_id,
        )
        if child.status != "completed":
            return ToolResult.error(f"Sub-council ended {child.status}: {child.error or 'no result'}")
        return ToolResult(
            (child.result or "")[:60_000],
            data={"child_run_id": str(child_id), "usd": usage.get("usd", 0)},
        )

    async def _stop(self, child_id: uuid.UUID) -> None:
        await self.manager.cancel(child_id)
        try:
            await self.manager.wait(child_id, 30)
        except TimeoutError:
            pass

    async def _relay(self, queue: asyncio.Queue[Envelope], parent: Any, child_id: uuid.UUID) -> None:
        while True:
            env = await queue.get()
            if env.type in RELAYED:
                await parent.sink.emit(
                    "subcouncil.event",
                    {
                        "child_run_id": str(child_id),
                        "type": env.type,
                        "seq": env.seq,
                        "actor": env.actor.model_dump(exclude_none=True),
                        "data": _brief(env.data),
                    },
                    actor=env.actor,
                )
            if env.type == "run.status" and env.data.get("status") in TERMINAL_STATUSES:
                return


def _actor(ctx: ToolCallContext):
    from argus.runtime.events import Actor

    return Actor(kind="agent", member_id=ctx.member_id, name=ctx.member_name, role=ctx.role)
