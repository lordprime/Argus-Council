"""How a council's agents, and the user, talk to each other: A2A messages and tasks.

Every member of a council has an A2A agent card, and everything said between members is an A2A
``Message`` (or, for delegated work, an A2A ``Task``) with a sender, recipients and the form of the
exchange. Inside one Argus the messages are delivered in process (no HTTP); each one is still
recorded in the protocol ledger as ``a2a`` (direction ``internal``) and on the run's timeline as an
``a2a.message`` event.

The eight forms of communication:

* ``direct``     one member to another (``send_message``), read at the recipient's next turn
* ``request``    ask a member and wait for the answer (``ask_agent``)
* ``broadcast``  one member to the whole council (``broadcast``)
* ``group``      one member to several, by name or role (``message_group``)
* ``delegate``   hand a member a task and get its result back, as an A2A task (``delegate_task``)
* ``topic``      publish to a topic; every subscriber receives it (``publish``, ``subscribe``)
* ``board``      the shared board (``notes_write``), shown to every member on every turn
* ``review``     ask a member to review your work; the critique comes back to you (``request_review``)

The user's messages during a run are A2A messages too (role ``user``): they reach the agents that
are working at their next turn and stay in every addressed member's instructions until the run ends.
Members answer the user with ``reply_to_user``.
"""

from __future__ import annotations

import contextvars
import hashlib
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from argus.core.logging import get_logger
from argus.runtime.context import RunCancelled
from argus.runtime.events import Actor
from argus.runtime.guardrails import GuardrailBreach
from argus.services import ledger

if TYPE_CHECKING:
    from collections.abc import Callable

    from argus.runtime.agent import MemberSpec
    from argus.runtime.context import RunContext
    from argus.runtime.orchestrator import CouncilOrchestrator

log = get_logger(__name__)

FORMS = ("direct", "request", "broadcast", "group", "delegate", "topic", "board", "review")
# Asks, delegations and reviews start a task for the recipient: they cost a model call each, so a
# run has a cap, and an agent answering one may ask in turn only so deep.
MAX_EXCHANGES = 40
MAX_DEPTH = 2
EVENT_TEXT = 8_000  # message text kept on the timeline event (the ledger keeps a digest)
BOARD_CHARS = 6_000
GUIDANCE_CHARS = 4_000
OWN_WORK_CHARS = 8_000
_ALL = {"council", "all", "everyone", "*"}
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("a2a_depth", default=0)

_TITLES = {"request": "Question", "delegate": "Delegated task", "review": "Review request"}
# The ways to communicate, as shown to a member that may use them (tool name(s), what it does).
_WAYS = (
    ("send_message", "tell one teammate something (they read it at their next turn)"),
    ("ask_agent", "ask a teammate a question and wait for the answer"),
    ("broadcast", "tell the whole council"),
    ("message_group", "tell several teammates, by name or role"),
    ("delegate_task", "hand a teammate a task and get the result back"),
    ("publish / subscribe", "topic channels"),
    ("notes_write", "post to the shared board below, which everyone sees"),
    ("request_review", "have a teammate review your work and act on the critique"),
    ("reply_to_user", "answer the user's messages"),
)


def text_of(message: dict[str, Any]) -> str:
    return "\n".join(p.get("text", "") for p in message.get("parts", []) if p.get("kind") == "text").strip()


def _ref(member: MemberSpec) -> dict[str, str]:
    return {"member_id": member.id, "name": member.name, "role": member.role}


USER_REF = {"kind": "user", "name": "You"}


def a2a_message(
    *,
    role: str,
    text: str,
    context_id: str,
    sender: dict[str, Any],
    recipients: list[dict[str, Any]],
    form: str,
    topic: str | None = None,
    reply_to: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """An A2A ``Message`` (protocol 0.3) with Argus's routing in ``metadata``."""
    meta: dict[str, Any] = {"from": sender, "to": recipients, "form": form}
    if topic:
        meta["topic"] = topic
    if reply_to:
        meta["inReplyTo"] = reply_to
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": f"msg_{uuid.uuid4().hex[:16]}",
        "contextId": context_id,
        "role": role,
        "parts": [{"kind": "text", "text": text}],
        "metadata": meta,
    }
    if task_id:
        msg["taskId"] = task_id
    return msg


def agent_card(member: MemberSpec, run_id: uuid.UUID) -> dict[str, Any]:
    """The A2A agent card of a council member (addressable only inside its run)."""
    from argus.protocols.a2a import PROTOCOL_VERSION

    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": member.name,
        "description": (member.system_prompt or member.role).strip().splitlines()[0][:300],
        "url": f"argus://runs/{run_id}/agents/{member.id}",
        "preferredTransport": "in-process",
        "version": "1",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "text/markdown"],
        "skills": [
            {
                "id": member.role,
                "name": member.role,
                "tags": [member.role],
                "description": f"The council's {member.role}",
            }
        ],
    }


class CouncilBus:
    """A council's A2A message bus: inboxes, topics, the board, user guidance and answer tasks."""

    def __init__(self, ctx: RunContext, orchestrator: CouncilOrchestrator) -> None:
        self.ctx = ctx
        self.o = orchestrator
        self.members: dict[str, MemberSpec] = {m.id: m for m in orchestrator.members}
        self.inbox: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.topics: dict[str, set[str]] = defaultdict(set)
        self.guidance: list[dict[str, Any]] = []  # the user's messages, kept for the whole run
        self.tasks: dict[str, dict[str, Any]] = {}
        self.exchanges = 0

    # ------------------------------------------------------------ addressing
    def cards(self) -> list[dict[str, Any]]:
        return [agent_card(m, self.ctx.run_id) for m in self.members.values()]

    def resolve(
        self, names: list[str], sender: MemberSpec | None = None
    ) -> tuple[list[MemberSpec], list[str]]:
        """Members named by name, id or role ("council" means everyone). Returns (found, unknown)."""
        found: dict[str, MemberSpec] = {}
        unknown: list[str] = []
        for raw in names:
            key = raw.strip().lower()
            if key in _ALL:
                found.update(self.members)
                continue
            hits = [m for m in self.members.values() if key in (m.name.lower(), m.id.lower(), m.role.lower())]
            if hits:
                found.update({m.id: m for m in hits})
            elif key:
                unknown.append(raw)
        if sender is not None:
            found.pop(sender.id, None)
        return list(found.values()), unknown

    def roster(self, exclude: str | None = None) -> str:
        return ", ".join(f"{m.name} ({m.role})" for m in self.members.values() if m.id != exclude)

    # ------------------------------------------------------------ recording
    async def _record(
        self,
        msg: dict[str, Any],
        *,
        actor: Actor,
        method: str = "message/send",
        direction: str = "internal",
        status: str = "ok",
        response: Any = None,
        step_id: str | None = None,
        t0: float | None = None,
    ) -> None:
        """Put one exchange in the protocol ledger (protocol ``a2a``) and on the run's timeline."""
        meta = msg.get("metadata", {})
        recipients = meta.get("to") or []
        peer = ", ".join(str(r.get("name")) for r in recipients) or "council"
        row = None
        try:
            row = await ledger.record(
                self.ctx.sessionmaker,
                owner_id=self.ctx.owner_id,
                protocol="a2a",
                direction=direction,
                peer=peer,
                method=method,
                status=status,
                request=msg,
                response=response,
                latency_ms=int((time.monotonic() - t0) * 1000) if t0 else 0,
                run_id=self.ctx.run_id,
                detail={
                    "purpose": "run",
                    "form": meta.get("form"),
                    "message_id": msg.get("messageId"),
                    "from": (meta.get("from") or {}).get("name"),
                    "to": [r.get("name") for r in recipients],
                    **({"task_id": msg["taskId"]} if msg.get("taskId") else {}),
                    **({"topic": meta["topic"]} if meta.get("topic") else {}),
                },
            )
        except Exception as exc:  # the ledger is a record, never a dependency of the conversation
            log.warning("a2a.ledger_failed", error=str(exc)[:200])
        shown = dict(msg)
        text = text_of(msg)
        if len(text) > EVENT_TEXT:
            shown["parts"] = [{"kind": "text", "text": text[:EVENT_TEXT] + "\n[truncated]"}]
        await self.ctx.sink.emit(
            "a2a.message",
            {
                "message": shown,
                "method": method,
                "status": status,
                "direction": direction,
                **({"ledger_id": row.id, "hash": row.hash} if row is not None else {}),
            },
            actor=actor,
            step_id=step_id,
        )

    async def _task_state(self, task: dict[str, Any], state: str, actor: Actor, step_id: str | None) -> None:
        task["status"] = {"state": state, "timestamp": datetime.now(UTC).isoformat()}
        await self.ctx.sink.emit(
            "a2a.task",
            {
                "task_id": task["id"],
                "state": state,
                "from": task["metadata"]["from"],
                "to": task["metadata"]["to"],
            },
            actor=actor,
            step_id=step_id,
        )

    # ------------------------------------------------------------ one-way forms
    async def send(
        self,
        sender: MemberSpec,
        recipients: list[MemberSpec],
        text: str,
        form: str,
        *,
        topic: str | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """Direct, group, broadcast and topic messages: delivered to each inbox, read next turn."""
        msg = a2a_message(
            role="agent",
            text=text,
            context_id=str(self.ctx.run_id),
            sender=_ref(sender),
            recipients=[_ref(r) for r in recipients],
            form=form,
            topic=topic,
        )
        for r in recipients:
            if r.id != sender.id:
                self.inbox[r.id].append(msg)
        await self._record(msg, actor=sender.actor, step_id=step_id)
        return msg

    def subscribe(self, member: MemberSpec, topics: list[str]) -> list[str]:
        for t in topics:
            self.topics[t.strip().lower()].add(member.id)
        return sorted(t for t, subs in self.topics.items() if member.id in subs)

    async def publish(
        self, sender: MemberSpec, topic: str, text: str, step_id: str | None = None
    ) -> tuple[dict, int]:
        key = topic.strip().lower()
        subscribers = [self.members[i] for i in sorted(self.topics.get(key, set())) if i != sender.id]
        msg = await self.send(sender, subscribers, text, "topic", topic=key, step_id=step_id)
        return msg, len(subscribers)

    async def post_board(
        self,
        author: MemberSpec,
        title: str,
        body: str,
        *,
        source: str | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """Add an entry to the shared board (the council's notes), which every member sees each turn."""
        note = {
            "id": len(self.ctx.notes) + 1,
            "title": title[:200],
            "body": body,
            "source": source,
            "tags": [],
            "author": author.name,
            "ts": datetime.now(UTC).isoformat(),
        }
        self.ctx.notes.append(note)
        await self.record_board(author, note, step_id=step_id)
        return note

    async def record_board(
        self, author: MemberSpec, note: dict[str, Any], step_id: str | None = None
    ) -> None:
        msg = a2a_message(
            role="agent",
            text=f"{note['title']}\n\n{note['body']}",
            context_id=str(self.ctx.run_id),
            sender=_ref(author),
            recipients=[{"name": "council", "kind": "board"}],
            form="board",
        )
        await self._record(msg, actor=author.actor, step_id=step_id)

    # ------------------------------------------------------------ forms that wait for an answer
    async def request(
        self,
        sender: MemberSpec,
        recipient: MemberSpec,
        text: str,
        form: str,
        *,
        step_id: str | None = None,
        expected: str = "",
        criteria: str = "",
    ) -> tuple[bool, str]:
        """Ask, delegate or request a review: the recipient works on it now and its answer comes back."""
        depth = _depth.get()
        if depth >= MAX_DEPTH:
            return False, (
                f"Not sent: this is already a {depth}-deep exchange. Answer with what you know instead of "
                "asking further."
            )
        if self.exchanges >= MAX_EXCHANGES:
            return False, f"Not sent: the council has used its {MAX_EXCHANGES} exchanges for this run."
        if recipient.id == sender.id:
            return False, "You cannot send this to yourself."
        self.exchanges += 1
        t0 = time.monotonic()
        task: dict[str, Any] | None = None
        if form == "delegate":
            task = {
                "kind": "task",
                "id": f"task_{uuid.uuid4().hex[:12]}",
                "contextId": str(self.ctx.run_id),
                "status": {"state": "submitted"},
                "history": [],
                "artifacts": [],
                "metadata": {"from": _ref(sender), "to": [_ref(recipient)]},
            }
            self.tasks[task["id"]] = task
        msg = a2a_message(
            role="agent",
            text=text,
            context_id=str(self.ctx.run_id),
            sender=_ref(sender),
            recipients=[_ref(recipient)],
            form=form,
            task_id=task["id"] if task else None,
        )
        if task:
            task["history"].append(msg)
        await self._record(
            msg, actor=sender.actor, method="tasks/send" if task else "message/send", step_id=step_id
        )
        if task:
            await self._task_state(task, "working", recipient.actor, step_id)
        prompt = self._prompt(sender, text, form, expected, criteria)
        # The question's digest is part of the task's name, so a resumed run replays an answer only
        # to the very same question (argus.runtime.resume keys tasks by phase, step, title, member).
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:10]
        token = _depth.set(depth + 1)
        try:
            # On a seat of its own: the recipient's own step may be running right now.
            runner = self.o.runner(recipient, seat=f"{recipient.id}-a2a-{msg['messageId'][4:]}")
            outcome = await runner.run_task(
                prompt,
                step_title=f"{_TITLES.get(form, 'Message')} from {sender.name} ({digest})",
                phase=f"a2a_{form}",
                context=self._own_work(recipient),
            )
            ok, answer = outcome.ok, outcome.text or ""
        except (RunCancelled, GuardrailBreach):
            raise  # the run is stopping: not an answer to pass on
        except Exception as exc:
            log.warning("a2a.answer_failed", form=form, error=str(exc)[:200])
            ok, answer = False, f"{recipient.name} could not answer: {type(exc).__name__}"
        finally:
            _depth.reset(token)
        reply = a2a_message(
            role="agent",
            text=answer or "(no answer)",
            context_id=str(self.ctx.run_id),
            sender=_ref(recipient),
            recipients=[_ref(sender)],
            form=form,
            reply_to=msg["messageId"],
            task_id=task["id"] if task else None,
        )
        if task:
            task["history"].append(reply)
            if ok:
                task["artifacts"].append(
                    {"artifactId": f"art_{uuid.uuid4().hex[:8]}", "parts": reply["parts"]}
                )
            await self._task_state(task, "completed" if ok else "failed", recipient.actor, step_id)
        await self._record(
            reply,
            actor=recipient.actor,
            status="ok" if ok else "error",
            response=None,
            step_id=step_id,
            t0=t0,
        )
        if form == "review" and ok:
            verdict = "revise" if "verdict: revise" in answer.lower() else "approve"
            await self.post_board(
                recipient, f"Review of {sender.name}'s work: {verdict}", answer[:1200], step_id=step_id
            )
        return ok, answer

    def _prompt(self, sender: MemberSpec, text: str, form: str, expected: str, criteria: str) -> str:
        who = f"{sender.name} ({sender.role})"
        if form == "delegate":
            return (
                f"{who} delegates this task to you over the council's A2A channel:\n\n{text}\n\n"
                f"Expected output: {expected or 'what the task asks for'}. Do the task fully, using your "
                "tools where they help, and return only the result."
            )
        if form == "review":
            return (
                f"{who} asks you to review their work.\nCriteria: "
                f"{criteria or 'correctness, evidence and citations, completeness, clarity'}\n\n"
                f"--- work under review ---\n{text}\n--- end ---\n\n"
                "Start with one line, exactly 'VERDICT: approve' or 'VERDICT: revise'. Then list the "
                "specific problems and how to fix each. Do not rewrite the work yourself."
            )
        return (
            f"{who} asks you, over the council's A2A channel:\n\n{text}\n\n"
            "Answer them directly from your role and the council's work so far. Be specific, cite "
            "sources you rely on, and say plainly what you do not know."
        )

    def _own_work(self, member: MemberSpec) -> str:
        done = [s for s in getattr(self.o, "steps", []) if s.assignee.id == member.id and s.output]
        text = "\n\n".join(f"## Your step: {s.title}\n{s.output}" for s in done)
        return text[-OWN_WORK_CHARS:]

    # ------------------------------------------------------------ the user
    async def user_message(
        self, text: str, target: MemberSpec | None, *, message: dict[str, Any] | None = None
    ) -> dict:
        """A message from the user, as an A2A message. It stays in the instructions of every member it
        addresses for the rest of the run (the immediate delivery is the run's user inbox)."""
        recipients = [_ref(target)] if target else [{"name": "council", "kind": "council"}]
        msg = a2a_message(
            role="user",
            text=text,
            context_id=str(self.ctx.run_id),
            sender=USER_REF,
            recipients=recipients,
            form="direct" if target else "broadcast",
        )
        if message and message.get("messageId"):
            msg["messageId"] = str(message["messageId"])[:80]
        msg["metadata"]["acknowledged_by"] = []
        self.guidance.append(msg)
        await self._record(msg, actor=Actor.user(), direction="inbound")
        return msg

    async def reply_to_user(
        self, sender: MemberSpec, text: str, in_reply_to: str | None, step_id: str | None
    ) -> dict:
        msg = a2a_message(
            role="agent",
            text=text,
            context_id=str(self.ctx.run_id),
            sender=_ref(sender),
            recipients=[USER_REF],
            form="direct",
            reply_to=in_reply_to,
        )
        for g in self.guidance:
            if (
                in_reply_to
                and g["messageId"] == in_reply_to
                and sender.name not in g["metadata"]["acknowledged_by"]
            ):
                g["metadata"]["acknowledged_by"].append(sender.name)
        await self._record(msg, actor=sender.actor, direction="outbound", step_id=step_id)
        return msg

    # ------------------------------------------------------------ what each member sees
    def take_inbox(self, member_id: str) -> list[dict[str, Any]]:
        return self.inbox.pop(member_id, [])

    def peek_inbox(self, member_id: str) -> list[dict[str, Any]]:
        """The member's unread messages, left in place (an answer task reads them, the member's own
        work still receives them)."""
        return list(self.inbox.get(member_id, ()))

    @staticmethod
    def render(msg: dict[str, Any]) -> str:
        meta = msg.get("metadata", {})
        sender = (meta.get("from") or {}).get("name", "someone")
        form = meta.get("form", "direct")
        topic = f" on topic '{meta['topic']}'" if meta.get("topic") else ""
        return f"[A2A {form} message {msg.get('messageId')} from {sender}{topic}]\n{text_of(msg)}"

    def prompt_for(self, member: MemberSpec, allowed: Callable[[str], bool] | None = None) -> str:
        """The council section of a member's instructions: who is who, how to talk (only the tools
        ``allowed`` lets the member call), the board and the user's guidance."""
        ways = [(t, line) for t, line in _WAYS if allowed is None or all(allowed(n) for n in t.split(" / "))]
        parts = [
            "\n## Your council (A2A agents)",
            "\n".join(
                f"- {m.name} ({m.role}){' (you)' if m.id == member.id else ''}" for m in self.members.values()
            ),
        ]
        if ways:
            parts.append(
                "\nYou can talk to them. Read what they send you and build on it; do not redo work they "
                "have done, and say so when you disagree. Ways to communicate:\n"
                + "\n".join(f"- {t}: {line}" for t, line in ways)
            )
        subs = sorted(t for t, ids in self.topics.items() if member.id in ids)
        if subs:
            parts.append(f"\nYou are subscribed to: {', '.join(subs)}.")
        board = self.board_text()
        if board:
            parts.append(f"\n## Council board (shared notes; read before you work)\n{board}")
        guidance = self.guidance_for(member)
        if guidance:
            parts.append(
                "\n## The user's instructions during this run (follow them; they override earlier plans)\n"
                f"{guidance}"
                + (
                    "\nAcknowledge each one you act on with reply_to_user (give its message id)."
                    if allowed is None or allowed("reply_to_user")
                    else ""
                )
            )
        return "\n".join(parts)

    def board_text(self) -> str:
        lines: list[str] = []
        used = 0
        for n in reversed(self.ctx.notes):
            body = str(n.get("body", ""))
            line = f"#{n['id']} [{n.get('author', '?')}] {n.get('title', '')}: {body[:700]}"
            if n.get("source"):
                line += f" (source: {n['source']})"
            if used + len(line) > BOARD_CHARS:
                lines.append(f"(+{len(self.ctx.notes) - len(lines)} older notes: notes_read shows them)")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(reversed(lines))

    def guidance_for(self, member: MemberSpec) -> str:
        mine = [
            g
            for g in self.guidance
            if any(r.get("kind") == "council" or r.get("member_id") == member.id for r in g["metadata"]["to"])
        ]
        text = "\n".join(f"- [{g['messageId']}] {text_of(g)}" for g in mine)
        return text[-GUIDANCE_CHARS:]
