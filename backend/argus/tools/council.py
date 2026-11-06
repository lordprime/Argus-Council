"""The council's communication tools: the eight forms of A2A exchange between members.

Every member gets these whatever its role's tool list says (``source = "council"``); the run's
``tools.allow`` / ``tools.deny`` guardrails still apply. See ``argus.runtime.council_bus``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argus.tools.base import StrList, Tool, ToolCallContext, ToolResult


class CouncilTool(Tool):
    source = "council"

    def timeout_for(self, args: BaseModel | None) -> float | None:
        return None  # a teammate's answer is bounded by its own turn and step limits

    @staticmethod
    def bus_and_sender(ctx: ToolCallContext) -> tuple[Any, Any]:
        bus = ctx.run.extra.get("council_bus")
        sender = bus.members.get(ctx.member_id) if bus is not None else None
        return bus, sender

    async def run(self, args: Any, ctx: ToolCallContext) -> ToolResult:
        bus, sender = self.bus_and_sender(ctx)
        if bus is None or sender is None:
            return ToolResult.error("There is no council to talk to in this run.")
        return await self.act(args, ctx, bus, sender)

    async def act(self, args: Any, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        raise NotImplementedError


def _unknown(bus: Any, names: list[str], sender: Any) -> ToolResult:
    return ToolResult.error(f"No council member called {', '.join(names)}. Members: {bus.roster(sender.id)}.")


class SendMessage(CouncilTool):
    name = "send_message"
    description = (
        "Send a message to one teammate (by name or role). They read it at their next turn. Use it to "
        "share a finding they need, flag a problem in their work, or coordinate."
    )

    class Args(BaseModel):
        to: str = Field(min_length=1, max_length=120, description="Teammate name or role")
        text: str = Field(min_length=1, max_length=8_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        found, unknown = bus.resolve([args.to], sender)
        if unknown or not found:
            return _unknown(bus, unknown or [args.to], sender)
        msg = await bus.send(sender, found[:1], args.text, "direct", step_id=ctx.step_id)
        return ToolResult(
            f"Sent to {found[0].name} ({msg['messageId']}).", data={"message_id": msg["messageId"]}
        )


class MessageGroup(CouncilTool):
    name = "message_group"
    description = (
        "Send one message to several teammates at once, named or by role (e.g. ['researcher', 'Analyst'])."
    )

    class Args(BaseModel):
        to: StrList = Field(min_length=1, max_length=20, description="Teammate names or roles")
        text: str = Field(min_length=1, max_length=8_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        found, unknown = bus.resolve(args.to, sender)
        if not found:
            return _unknown(bus, unknown or args.to, sender)
        msg = await bus.send(sender, found, args.text, "group", step_id=ctx.step_id)
        note = f" Unknown: {', '.join(unknown)}." if unknown else ""
        return ToolResult(
            f"Sent to {', '.join(m.name for m in found)}.{note}", data={"message_id": msg["messageId"]}
        )


class Broadcast(CouncilTool):
    name = "broadcast"
    description = (
        "Tell the whole council something every member should know now (a key finding, a change of plan)."
    )

    class Args(BaseModel):
        text: str = Field(min_length=1, max_length=8_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        found, _ = bus.resolve(["council"], sender)
        msg = await bus.send(sender, found, args.text, "broadcast", step_id=ctx.step_id)
        return ToolResult(f"Broadcast to {len(found)} teammates.", data={"message_id": msg["messageId"]})


class AskAgent(CouncilTool):
    name = "ask_agent"
    description = (
        "Ask a teammate a question and wait for their answer. Use it when their role or their work "
        "holds what you need, instead of guessing or redoing it."
    )

    class Args(BaseModel):
        to: str = Field(min_length=1, max_length=120, description="Teammate name or role")
        question: str = Field(min_length=1, max_length=8_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        found, unknown = bus.resolve([args.to], sender)
        if not found:
            return _unknown(bus, unknown or [args.to], sender)
        ok, answer = await bus.request(sender, found[0], args.question, "request", step_id=ctx.step_id)
        return ToolResult(f"{found[0].name} answered:\n{answer}" if ok else answer, is_error=not ok)


class DelegateTask(CouncilTool):
    name = "delegate_task"
    description = (
        "Hand a teammate a self-contained task (an A2A task) and get the result back when they finish. "
        "Give them everything they need; they do not see your conversation."
    )

    class Args(BaseModel):
        to: str = Field(min_length=1, max_length=120, description="Teammate name or role")
        task: str = Field(min_length=1, max_length=12_000)
        expected_output: str = Field(default="", max_length=1_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        found, unknown = bus.resolve([args.to], sender)
        if not found:
            return _unknown(bus, unknown or [args.to], sender)
        ok, result = await bus.request(
            sender, found[0], args.task, "delegate", step_id=ctx.step_id, expected=args.expected_output
        )
        return ToolResult(f"{found[0].name} completed the task:\n{result}" if ok else result, is_error=not ok)


class RequestReview(CouncilTool):
    name = "request_review"
    description = (
        "Have a teammate review your work before you hand it in. Their verdict and critique come back "
        "to you: fix what they found, or say why not."
    )

    class Args(BaseModel):
        to: str = Field(min_length=1, max_length=120, description="Teammate name or role, e.g. 'critic'")
        work: str = Field(min_length=1, max_length=20_000)
        criteria: str = Field(default="", max_length=1_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        found, unknown = bus.resolve([args.to], sender)
        if not found:
            return _unknown(bus, unknown or [args.to], sender)
        ok, critique = await bus.request(
            sender, found[0], args.work, "review", step_id=ctx.step_id, criteria=args.criteria
        )
        if not ok:
            return ToolResult.error(critique)
        return ToolResult(
            f"Review from {found[0].name}:\n{critique}\n\nAddress every point before you finish."
        )


class Subscribe(CouncilTool):
    name = "subscribe"
    description = (
        "Subscribe to council topics (e.g. 'sources', 'risks'); messages published to them reach you."
    )

    class Args(BaseModel):
        topics: StrList = Field(min_length=1, max_length=20)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        subs = bus.subscribe(sender, args.topics)
        return ToolResult(f"Subscribed. Your topics: {', '.join(subs)}.")


class Publish(CouncilTool):
    name = "publish"
    description = "Publish a message to a council topic; every member subscribed to it receives it."

    class Args(BaseModel):
        topic: str = Field(min_length=1, max_length=60)
        text: str = Field(min_length=1, max_length=8_000)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        msg, n = await bus.publish(sender, args.topic, args.text, step_id=ctx.step_id)
        who = f"{n} subscriber{'s' if n != 1 else ''}" if n else "no subscribers yet (it is still recorded)"
        return ToolResult(f"Published to '{args.topic}': {who}.", data={"message_id": msg["messageId"]})


class ReplyToUser(CouncilTool):
    name = "reply_to_user"
    description = (
        "Answer or acknowledge a message the user sent during the run (give its message id). Say what "
        "you are doing about it."
    )

    class Args(BaseModel):
        text: str = Field(min_length=1, max_length=8_000)
        message_id: str | None = Field(default=None, max_length=80)

    async def act(self, args: Args, ctx: ToolCallContext, bus: Any, sender: Any) -> ToolResult:
        await bus.reply_to_user(sender, args.text, args.message_id, ctx.step_id)
        return ToolResult("Sent to the user.")


COUNCIL_TOOL_NAMES = frozenset(
    {
        "send_message",
        "message_group",
        "broadcast",
        "ask_agent",
        "delegate_task",
        "request_review",
        "subscribe",
        "publish",
        "reply_to_user",
    }
)


def council_tools() -> list[Tool]:
    return [
        SendMessage(),
        MessageGroup(),
        Broadcast(),
        AskAgent(),
        DelegateTask(),
        RequestReview(),
        Subscribe(),
        Publish(),
        ReplyToUser(),
    ]
