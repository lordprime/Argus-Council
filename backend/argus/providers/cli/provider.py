"""Provider adapter for CLI agents: one instance per (user, CLI).

Session handling: a council seat keeps one vendor session for the whole run. The first turn
starts a session (claude/gemini with an id Argus picks, codex with the id it reports in
``thread.started``); later turns resume it and send only what the CLI has not seen yet (tool
results, corrections, the next task). A seat can instead be pinned to an existing session of the
same user (``config.cli_session``), which is checked against the user's own records and the
runner's per-user session index before it is resumed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from argus.core.logging import get_logger
from argus.providers.base import Provider
from argus.providers.cli.client import RunnerTransport, RunnerUnavailable
from argus.providers.cli.errors import CLIError
from argus.providers.cli.parsers import make_parser
from argus.providers.cli.toolproto import (
    extract_tool_calls,
    message_hash,
    render_delta,
    render_tools,
    render_transcript,
    visible_prefix,
)
from argus.providers.pricing import estimate_cost
from argus.providers.types import CompletionRequest, CompletionResponse, Message, StreamEvent, Usage
from argus.runner.process import redact
from argus.runner.specs import UUID_RE, InvokeSpec

log = get_logger(__name__)

CLI_PROVIDERS: dict[str, str] = {"claude-cli": "claude", "codex-cli": "codex", "gemini-cli": "gemini"}
SUGGESTED_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("sonnet", "opus", "haiku", "default"),
    "codex": ("default",),
    "gemini": ("default", "gemini-2.5-pro", "gemini-2.5-flash"),
}


def cli_for_provider(name: str) -> str | None:
    return CLI_PROVIDERS.get(name)


class CLIBackend(Protocol):
    """What an adapter needs from the app: the runner, the user's keys and session records."""

    transport: RunnerTransport
    allow_subscription: bool
    timeout_s: int

    async def api_key(self, owner_id: uuid.UUID, cli: str) -> str | None: ...

    async def session_owned(self, owner_id: uuid.UUID, cli: str, session_id: str) -> bool: ...

    async def remember_session(
        self, owner_id: uuid.UUID, cli: str, session_id: str, run_id: str | None, member_id: str | None
    ) -> None: ...


@dataclass
class SeatSession:
    session_id: str | None = None
    sent: list[str] = field(default_factory=list)  # hashes of the conversation the CLI has seen
    system: str = ""
    reported_cost: float | None = None  # claude reports a cumulative cost per session
    pinned: bool = False


class CLIProvider(Provider):
    def __init__(self, cli: str, owner_id: uuid.UUID, backend: CLIBackend) -> None:
        self.cli = cli
        self.kind = f"{cli}-cli"
        super().__init__(self.kind)
        self.owner_id = owner_id
        self.backend = backend
        self._seats: dict[tuple[str, str], SeatSession] = {}

    async def list_models(self) -> list[str]:
        return list(SUGGESTED_MODELS.get(self.cli, ()))

    # ------------------------------------------------------------------ helpers
    def seat_session(self, run_id: str, member_id: str) -> SeatSession | None:
        return self._seats.get((run_id, member_id))

    async def _seat(self, meta: dict[str, Any]) -> tuple[tuple[str, str] | None, SeatSession | None]:
        run_id, member_id = meta.get("run_id"), meta.get("member_id")
        if not run_id or not member_id:
            return None, None  # overseer reviews, probes: one-shot, nothing persisted
        key = (str(run_id), str(member_id))
        seat = self._seats.get(key)
        if seat is None:
            seat = SeatSession()
            pinned = meta.get("cli_session")
            if pinned:
                pinned = str(pinned).lower()
                if not UUID_RE.match(pinned) or not await self._owns(pinned):
                    raise CLIError(
                        "The seat's CLI session does not exist for this user",
                        provider=self.name,
                        category="session",
                    )
                seat.session_id, seat.pinned = pinned, True
            self._seats[key] = seat
        return key, seat

    async def _owns(self, session_id: str) -> bool:
        if not await self.backend.session_owned(self.owner_id, self.cli, session_id):
            return False
        try:
            return await self.backend.transport.has_session(str(self.owner_id), self.cli, session_id)
        except RunnerUnavailable as exc:
            raise CLIError(str(exc), provider=self.name, category="unavailable") from exc

    def forget_run(self, run_id: str) -> None:
        for key in [k for k in self._seats if k[0] == run_id]:
            self._seats.pop(key)

    # ------------------------------------------------------------------ provider API
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        response: CompletionResponse | None = None
        async for event in self.stream(request):
            if event.type == "done":
                response = event.response
        assert response is not None
        return response

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        meta = request.metadata or {}
        key, seat = await self._seat(meta)
        system = "\n\n".join(p for p in (request.system_prompt, render_tools(request.tools)) if p)
        conv = request.conversation
        hashes = [message_hash(m) for m in conv]
        resume = bool(seat and seat.session_id)
        if (
            seat
            and resume
            and seat.sent
            and hashes[: len(seat.sent)] == seat.sent
            and len(hashes) > len(seat.sent)
        ):
            prompt = render_delta(conv[len(seat.sent) :])
        else:
            prompt = render_transcript(conv)
        if resume and seat and system != seat.system:
            # The CLI keeps the system prompt it started with; restate what changed (standing orders).
            prompt = f"<updated_system_instructions>\n{system}\n</updated_system_instructions>\n\n{prompt}"
        if not prompt.strip():
            prompt = "Continue."
        session_id = seat.session_id if seat else None
        if seat and not resume and self.cli in ("claude", "gemini"):
            session_id = str(uuid.uuid4())
        api_key = await self.backend.api_key(self.owner_id, self.cli)
        if not api_key and not self.backend.allow_subscription:
            raise CLIError(
                f"{self.cli} needs an API key on this instance (subscription logins are disabled)",
                provider=self.name,
                category="auth",
            )
        spec = InvokeSpec(
            user_id=str(self.owner_id),
            cli=self.cli,  # type: ignore[arg-type]
            prompt=prompt,
            system_prompt="" if resume else system,
            model=request.model,
            effort=request.effort if self.cli == "claude" else None,
            session_id=session_id,
            resume=resume,
            persist=seat is not None,
            run_key=_run_key(meta) if seat is not None else None,
            api_key=api_key,
            timeout_s=self.backend.timeout_s,
        )
        parser = make_parser(self.cli)
        exit_record: dict[str, Any] | None = None
        shown = ""
        visible = 0
        try:
            async for raw in self.backend.transport.invoke(spec):
                if raw.get("type") == "argus.exit":
                    exit_record = raw
                    break
                for event in parser.feed(raw):
                    if event.type != "text":
                        yield event
                        continue
                    shown += event.text
                    upto = visible_prefix(shown)
                    if upto > visible:
                        yield StreamEvent(type="text", text=shown[visible:upto])
                        visible = upto
        except RunnerUnavailable as exc:
            raise CLIError(str(exc), provider=self.name, category="unavailable") from exc
        result = parser.finish(exit_record)
        if result.failure is not None:
            if seat is not None and result.failure.category == "session" and not seat.pinned:
                seat.session_id, seat.sent = None, []  # start over next time
            raise CLIError(
                # CLIs echo key fragments in errors ("Incorrect API key provided: sk-..."); these
                # messages reach logs, run events and health, so redact them like the runner does.
                f"{self.cli} CLI: {redact(result.failure.message, [api_key] if api_key else [])}",
                provider=self.name,
                category=result.failure.category,
            )
        allowed = {t.name for t in request.tools}
        content, calls = extract_tool_calls(result.text, allowed)
        model = request.model if request.model and request.model != "default" else (result.model or self.cli)
        usage = result.usage
        estimate = estimate_cost(result.model or model, usage)
        turn_cost = estimate
        if result.reported_cost_usd is not None:
            if seat is not None and resume:
                if seat.reported_cost is not None:
                    turn_cost = max(0.0, result.reported_cost_usd - seat.reported_cost)
                # first turn on a pinned session: the reported total includes earlier spend; keep the estimate
            else:
                turn_cost = result.reported_cost_usd
            if seat is not None:
                seat.reported_cost = result.reported_cost_usd
        billing = "api_key" if api_key else "subscription"
        usage = Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            # Subscription turns cost no API dollars; the estimate is reported separately.
            cost_usd=round(turn_cost, 8) if billing == "api_key" else 0.0,
        )
        response = CompletionResponse(
            provider=self.name,
            model=model,
            content=content,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            usage=usage,
            thinking=result.thinking,
            billing=billing,
            estimated_cost_usd=round(turn_cost, 8),
            session_id=result.session_id or session_id,
        )
        if seat is not None and key is not None:
            new_id = result.session_id or session_id
            if new_id and UUID_RE.match(new_id):
                if new_id != seat.session_id:
                    await self.backend.remember_session(self.owner_id, self.cli, new_id.lower(), *key)
                seat.session_id = new_id.lower()
            seat.sent = [*hashes, message_hash(Message(role="assistant", content=content))]
            seat.system = system if not resume else seat.system or system
        yield StreamEvent(type="done", response=response)


def _run_key(meta: dict[str, Any]) -> str:
    run_id = str(meta.get("run_id") or "")
    member = str(meta.get("member_id") or "")
    clean = "".join(ch for ch in f"{run_id}-{member}" if ch.isalnum() or ch in "-_")
    return clean[:100] or "run"
