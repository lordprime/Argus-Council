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

from argus.core.logging import get_logger
from argus.db.models import Run
from argus.providers.types import Usage
from argus.runtime.events import TERMINAL_STATUSES, Envelope
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from argus.tools.base import Tool, ToolCallContext, ToolResult

if TYPE_CHECKING:
    from argus.runtime.manager import RunManager

log = get_logger(__name__)

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

    def timeout_for(self, args: BaseModel | None) -> float | None:
        # The child enforces its own max_runtime_minutes; a generic tool timeout would kill it
        # before its spend is charged back.
        return None

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
        usd, tokens = child_budget(parent.engine, child_rails)
        if usd is not None and usd <= 0:
            return ToolResult.error("No budget left for a sub-council.")
        if tokens is not None and tokens <= 0:
            return ToolResult.error("No token budget left for a sub-council.")
        child_rails.budget.usd, child_rails.budget.tokens = usd, tokens
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
        child_engine: GuardrailEngine | None = None
        try:
            try:
                live = await self.manager.start(
                    child_id,
                    depth=parent.depth + 1,
                    listeners=[queue.put_nowait],
                    budget_parent=parent.engine,
                )
            except Exception as exc:  # the child row is already marked failed
                log.warning("subcouncil.start_failed", child_run_id=str(child_id), error=str(exc)[:200])
            else:
                child_engine = live.ctx.engine
                parent.children.add(child_id)  # cancelling the parent cancels the child
                try:
                    await asyncio.wait_for(
                        self._relay(queue, parent, child_id), child_rails.max_runtime_minutes * 60
                    )
                except TimeoutError:
                    pass  # _finish stops it
        except asyncio.CancelledError:
            # The parent's task is being cancelled: still stop the child and charge its spend back
            # before letting the cancellation through, so the parent's final usage includes it.
            await asyncio.shield(asyncio.ensure_future(self._finish(ctx, child_id, child_engine)))
            raise
        child, usage = await self._finish(ctx, child_id, child_engine)
        if child is None or child.status != "completed":
            status = child.status if child else "unknown"
            return ToolResult.error(
                f"Sub-council ended {status}: {(child.error if child else None) or 'no result'}"
            )
        return ToolResult(
            (child.result or "")[:60_000],
            data={"child_run_id": str(child_id), "usd": usage.get("usd", 0)},
        )

    async def _finish(
        self, ctx: ToolCallContext, child_id: uuid.UUID, child_engine: GuardrailEngine | None
    ) -> tuple[Run | None, dict[str, Any]]:
        """Stop the child if needed, charge its spend to the parent, emit ``subcouncil.completed``."""
        parent = ctx.run
        parent.children.discard(child_id)
        await self._stop(child_id)
        child: Run | None = None
        try:
            async with parent.sessionmaker() as s:
                child = await s.get(Run, child_id)
        except Exception as exc:
            log.warning("subcouncil.read_failed", child_run_id=str(child_id), error=str(exc)[:200])
        usage = dict((child.usage if child else None) or {})
        if child_engine is not None:
            # The child's spend already counts against this run while it runs; settle it for good.
            charged = parent.engine.settle_child(child_engine)
            usage = usage or {
                "usd": round(charged.cost_usd, 6),
                "tokens_in": charged.input_tokens,
                "tokens_out": charged.output_tokens,
            }
        else:
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
                "status": child.status if child else "unknown",
                "error": child.error if child else None,
                "usage": usage,
                "tool_call_id": ctx.tool_call_id,
            },
            actor=_actor(ctx),
            step_id=ctx.step_id,
        )
        return child, usage

    async def _stop(self, child_id: uuid.UUID) -> None:
        await self.manager.cancel(child_id)
        try:
            await self.manager.wait(child_id, 30)
        except TimeoutError:
            log.warning("subcouncil.stop_timeout", child_run_id=str(child_id))

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


def child_budget(parent: GuardrailEngine, rails: Guardrails) -> tuple[float | None, int | None]:
    """Caps for a new child: its own (clamped) limits, never above what the parent has left now.

    "Left" counts the spend of the parent's sub-councils that are still running, so children
    spawned in parallel cannot each be handed the parent's whole remaining budget.
    """
    used = parent.total_usage
    usd = rails.budget.usd
    if parent.rails.budget.usd is not None:
        left = max(parent.rails.budget.usd - used.cost_usd, 0.0)
        usd = left if usd is None else min(usd, left)
    tokens = rails.budget.tokens
    if parent.rails.budget.tokens is not None:
        left_tokens = max(parent.rails.budget.tokens - used.total_tokens, 0)
        tokens = left_tokens if tokens is None else min(tokens, left_tokens)
    return usd, tokens


def _actor(ctx: ToolCallContext):
    from argus.runtime.events import Actor

    return Actor(kind="agent", member_id=ctx.member_id, name=ctx.member_name, role=ctx.role)
