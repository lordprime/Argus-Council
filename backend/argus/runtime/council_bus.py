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

The debate adds forms for the claim ledger (``make_claim``, ``challenge``, ``respond_to_challenge``,
``resolve_challenge``) and for the conclusion (``position``, ``signature``). Each carries a ``data``
part ``{"type": "argus.<form>", ...}`` beside its text (docs/design/parallel-council.md, section 2).

The user's messages during a run are A2A messages too (role ``user``): they reach the agents that
are working at their next turn and stay in every addressed member's instructions until the run ends.
Members answer the user with ``reply_to_user``.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
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
DEBATE_FORMS = ("claim", "challenge", "response", "resolution", "position", "signature")
DEMANDS = ("proof", "counterexample", "clarification")
OPEN_CHALLENGE = frozenset({"open", "unsupported"})  # waiting for the target's answer
CLAIM_DIGEST = 12  # claims shown in each member's instructions
# Phrases that would give away a secret stance (the devil's advocate), with neutral replacements.
_GIVEAWAYS = (
    (re.compile(r"\bdevil(?:'|" + chr(0x2019) + r")?s[\s-]+advocates?\b", re.I), "sceptic"),
    (
        re.compile(
            r"\b(?:I\s+(?:was|am|have\s+been)\s+|I'm\s+)?assigned\s+(?:the\s+role\s+)?to\s+(?:argue|oppose)\b",
            re.I,
        ),
        "inclined to question",
    ),
    (
        re.compile(
            r"\bmy\s+(?:assigned\s+)?(?:role|job|task)\s+is\s+to\s+(?:oppose|argue\s+against|disagree)\b",
            re.I,
        ),
        "I have doubts, so I question",
    ),
    (re.compile(r"\b(?:argue|arguing)\s+the\s+other\s+side\b", re.I), "question this"),
    (re.compile(r"\bstanding\s+orders?\b", re.I), "view"),
)


def conceal(member: MemberSpec | None, text: str) -> str:
    """``text`` as ``member`` may say it to the council: a member with a secret stance never gives it
    away (the phrases are replaced, and nobody is told)."""
    if member is None or not getattr(member, "stance", None) or not text:
        return text
    for pattern, neutral in _GIVEAWAYS:
        text = pattern.sub(neutral, text)
    return text


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:10]


# Asks, delegations and reviews start a task for the recipient: they cost a model call each, so a
# run has a cap, and an agent answering one may ask in turn only so deep.
MAX_EXCHANGES = 40
MAX_DEPTH = 2
EVENT_TEXT = 8_000  # message text kept on the timeline event (the ledger keeps a digest)
BOARD_CHARS = 6_000
GUIDANCE_CHARS = 4_000
OWN_WORK_CHARS = 8_000
BOARD_EVENT = "board.note"  # every note on the board, durable (argus.runtime.resume reads them)
LIVE_TAIL_CHARS = 300  # the tail of each member's latest thought in "Working now"
LIVE_CHARS = 2_000  # the whole "Working now" section
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
# The debate's ways (only while the council debates).
_DEBATE_WAYS = (
    ("make_claim", "put a finding on record as a claim, with its evidence"),
    ("challenge", "challenge a claim or a teammate's work, demanding proof, a counterexample or clarity"),
    ("respond_to_challenge", "answer a challenge against you: defend (with evidence), concede or revise"),
    ("resolve_challenge", "accept or reject the answer to a challenge you raised"),
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
        # member id -> {task, tail, ts}: what each member is working on right now ("Working now").
        self.live: dict[str, dict[str, Any]] = {}
        # The claim ledger (debate): claim id -> claim, challenge id -> challenge.
        self.claims: dict[str, dict[str, Any]] = {}
        self.challenges: dict[str, dict[str, Any]] = {}
        self.round = 0  # the deliberation round under way (0: while the work runs)
        self._ops: set[str] = set()  # answers and resolutions already applied (replays are no-ops)

    # ------------------------------------------------------------ addressing
    def join(self, member: MemberSpec) -> dict[str, Any]:
        """Add a member that joins while the run works (a helper); returns its A2A agent card."""
        self.members[member.id] = member
        return agent_card(member, self.ctx.run_id)

    def leave(self, member_id: str) -> None:
        """Remove a member that has finished (a helper): it is no longer addressable."""
        self.members.pop(member_id, None)
        self.live.pop(member_id, None)
        self.inbox.pop(member_id, None)
        for subs in self.topics.values():
            subs.discard(member_id)

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
            shown["parts"] = [{"kind": "text", "text": text[:EVENT_TEXT] + "\n[truncated]"}] + [
                p for p in msg.get("parts", []) if p.get("kind") != "text"
            ]
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
            text=conceal(sender, text),
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
            "title": conceal(author, title)[:200],
            "body": conceal(author, body),
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
        # notes_write puts the note on the board itself: what the board shows must give nothing away.
        note["title"] = conceal(author, str(note.get("title", "")))
        note["body"] = conceal(author, str(note.get("body", "")))
        msg = a2a_message(
            role="agent",
            text=f"{note['title']}\n\n{note['body']}",
            context_id=str(self.ctx.run_id),
            sender=_ref(author),
            recipients=[{"name": "council", "kind": "board"}],
            form="board",
        )
        await self._record(msg, actor=author.actor, step_id=step_id)
        # The board as a record of its own: a resumed run rebuilds it, and the next run inherits it.
        await self.ctx.sink.emit(BOARD_EVENT, {"note": dict(note)}, actor=author.actor, step_id=step_id)

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
        text = conceal(sender, text)
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
            ok, answer = outcome.ok, conceal(recipient, outcome.text or "")
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
            if verdict == "revise" and self.debating:
                # The review is enforced: the reviewed member must answer it before it finishes.
                await self.open_challenge(
                    recipient, sender.id, f"Review: {answer[:3_000]}", "clarification", step_id=step_id
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
        if self.debating:
            ways += [(t, line) for t, line in _DEBATE_WAYS if allowed is None or allowed(t)]
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
        debate = self.debate_text(member) if self.debating else ""
        if debate:
            parts.append(debate)
        working = self.working_now(member)
        if working:
            parts.append(
                "\n## Working now (your teammates' current work; correct or build on it as it happens)\n"
                f"{working}"
            )
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
        """The board as members see it: compact graph summaries when the council graph is on
        (argus.runtime.graph), otherwise the raw notes (``board_text_raw``)."""
        from argus.runtime.graph import graph_for

        graph = graph_for(self.ctx)
        if graph is not None:
            return graph.board_text(self.ctx.notes, BOARD_CHARS)
        return self.board_text_raw()

    def board_text_raw(self) -> str:
        lines: list[str] = []
        used = 0
        for n in reversed(self.ctx.notes):
            body = str(n.get("body", ""))
            earlier = " (earlier run)" if n.get("inherited_from") else ""
            line = f"#{n['id']} [{n.get('author', '?')}{earlier}] {n.get('title', '')}: {body[:700]}"
            if n.get("source"):
                line += f" (source: {n['source']})"
            if used + len(line) > BOARD_CHARS:
                lines.append(f"(+{len(self.ctx.notes) - len(lines)} older notes: notes_read shows them)")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(reversed(lines))

    # ------------------------------------------------------------ live view
    async def set_live(self, member: MemberSpec, task: str, tail: str) -> None:
        """Record the tail of ``member``'s latest thought on ``task`` (from AgentRunner._think)."""
        # Teammates read this tail, so a secret stance is concealed here too.
        text = " ".join(conceal(member, tail or "").split())
        if len(text) > LIVE_TAIL_CHARS:
            text = "…" + text[-LIVE_TAIL_CHARS:]
        self.live[member.id] = {"task": task[:160], "tail": text, "ts": datetime.now(UTC).isoformat()}
        await self._emit_live(member, task[:160], text)

    async def clear_live(self, member: MemberSpec, task: str) -> None:
        """The member finished ``task``: drop it from "Working now" (unless it moved on already)."""
        entry = self.live.get(member.id)
        if entry is None or entry["task"] != task[:160]:
            return
        self.live.pop(member.id, None)
        await self._emit_live(member, None, "")

    async def _emit_live(self, member: MemberSpec, task: str | None, tail: str) -> None:
        try:
            await self.ctx.sink.emit(
                "member.live", {"member_id": member.id, "task": task, "tail": tail}, actor=member.actor
            )
        except Exception as exc:  # a view, never a dependency of the work
            log.warning("council.live_failed", error=str(exc)[:200])

    def working_now(self, member: MemberSpec) -> str:
        """What the other members are doing right now, newest first, within ``LIVE_CHARS``."""
        lines: list[str] = []
        used = 0
        entries = sorted(self.live.items(), key=lambda kv: kv[1]["ts"], reverse=True)
        for mid, entry in entries:
            other = self.members.get(mid)
            if other is None or mid == member.id:
                continue
            line = f"- {other.name} ({other.role}) on '{entry['task']}': {entry['tail'] or '(starting)'}"
            if used + len(line) > LIVE_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def guidance_for(self, member: MemberSpec) -> str:
        mine = [
            g
            for g in self.guidance
            if any(r.get("kind") == "council" or r.get("member_id") == member.id for r in g["metadata"]["to"])
        ]
        text = "\n".join(f"- [{g['messageId']}] {text_of(g)}" for g in mine)
        return text[-GUIDANCE_CHARS:]

    # ------------------------------------------------------------ the claim ledger (debate)
    # Claims, challenges, answers and resolutions, each an A2A message with a ``data`` part. Ids are
    # derived from the content, and an answer or resolution already applied is not applied again,
    # so a resumed run that replays a debate task does not record its moves twice.
    @property
    def debating(self) -> bool:
        """Whether this council debates (top-level runs with ``debate.enabled``; sub-councils keep
        their own plan and vote)."""
        return bool(self.ctx.engine.rails.debate.enabled) and self.ctx.depth == 0

    def _seen(self, *parts: Any) -> bool:
        key = _digest(*parts)
        if key in self._ops:
            return True
        self._ops.add(key)
        return False

    async def debate_message(
        self,
        sender: MemberSpec,
        recipients: list[MemberSpec],
        text: str,
        form: str,
        data: dict[str, Any],
        *,
        audience: str | None = None,
        reply_to: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """One debate move as an A2A message: a text part for people and a ``data`` part
        ``{"type": "argus.<form>", ...}``. ``audience`` ("board" or "council") addresses the whole
        council without filling inboxes; otherwise each recipient reads it at its next turn."""
        msg = a2a_message(
            role="agent",
            text=conceal(sender, text),
            context_id=str(self.ctx.run_id),
            sender=_ref(sender),
            recipients=[{"name": "council", "kind": audience}] if audience else [_ref(r) for r in recipients],
            form=form,
            reply_to=reply_to,
            task_id=task_id,
        )
        msg["parts"].append({"kind": "data", "data": {"type": f"argus.{form}", **data}})
        if not audience:
            for r in recipients:
                if r.id != sender.id:
                    self.inbox[r.id].append(msg)
        method = "tasks/send" if form == "challenge" and task_id else "message/send"
        await self._record(msg, actor=sender.actor, method=method, step_id=step_id)
        return msg

    async def set_claim_status(self, claim: dict[str, Any], status: str, step_id: str | None = None) -> None:
        if claim.get("status") == status:
            return
        claim["status"] = status
        await self.ctx.sink.emit(
            "debate.claim_status", {"claim_id": claim["claim_id"], "status": status}, step_id=step_id
        )

    async def make_claim(
        self,
        author: MemberSpec,
        statement: str,
        evidence: Any = None,
        confidence: Any = None,
        *,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """Put a claim on record (``clm_…``), broadcast on the board."""
        statement = " ".join(conceal(author, statement).split())[:2_000]
        cid = f"clm_{_digest(author.id, statement.lower())}"
        if cid in self.claims:
            return self.claims[cid]
        ev = _evidence(evidence, author)
        claim: dict[str, Any] = {
            "claim_id": cid,
            "author_id": author.id,
            "author": author.name,
            "statement": statement,
            "evidence": ev,
            "confidence": _confidence(confidence),
            "status": "open",
            "round": self.round,
        }
        self.claims[cid] = claim
        fields = ("claim_id", "author_id", "statement", "evidence", "confidence", "status")
        data = {k: claim[k] for k in fields}
        text = f"Claim {cid}: {statement}" + (
            "\nEvidence: " + "; ".join(_ev_text(e) for e in ev) if ev else "\n(no evidence given)"
        )
        msg = await self.debate_message(author, [], text, "claim", data, audience="board", step_id=step_id)
        claim["message_id"] = msg["messageId"]
        await self.ctx.sink.emit("debate.claim", data, actor=author.actor, step_id=step_id)
        return claim

    async def open_challenge(
        self,
        challenger: MemberSpec,
        target: str,
        objection: str,
        demand: str = "proof",
        *,
        step_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Challenge a claim (by id) or a member's work (by name, role or id): an A2A task waiting
        for the target's answer (``input-required``). Returns (challenge or None, what happened)."""
        if not self.debating:
            return None, "The council is not debating in this run."
        key = str(target).strip()
        claim = self.claims.get(key)
        who: MemberSpec | None
        if claim is not None:
            who = self.members.get(claim["author_id"])
        else:
            found, _ = self.resolve([key]) if key and key.lower() not in _ALL else ([], [])
            others = [m for m in found if m.id != challenger.id]
            who = others[0] if others else (found[0] if found else None)
        if who is None:
            known = ", ".join(list(self.claims)[-8:]) or "none yet"
            return None, (
                f"No claim or member called '{key}'. Claims: {known}. Members: {self.roster(challenger.id)}."
            )
        if who.id == challenger.id:
            return None, "You cannot challenge your own claim or work."
        limit = self.ctx.engine.rails.debate.max_challenges
        if len(self.challenges) >= limit:
            return None, f"Not opened: the council has used its {limit} challenges for this run."
        objection = conceal(challenger, str(objection)).strip()[:4_000]
        if not objection:
            return None, "Say what is wrong: the objection is empty."
        demand = demand if demand in DEMANDS else "clarification"
        chid = f"chl_{_digest(challenger.id, claim['claim_id'] if claim else who.id, objection)}"
        if chid in self.challenges:
            return self.challenges[chid], f"Challenge {chid} is already on record."
        task: dict[str, Any] = {
            "kind": "task",
            "id": f"task_{uuid.uuid4().hex[:12]}",
            "contextId": str(self.ctx.run_id),
            "status": {"state": "submitted"},
            "history": [],
            "artifacts": [],
            "metadata": {"from": _ref(challenger), "to": [_ref(who)], "form": "challenge"},
        }
        self.tasks[task["id"]] = task
        ch: dict[str, Any] = {
            "challenge_id": chid,
            "claim_id": claim["claim_id"] if claim else None,
            "challenger_id": challenger.id,
            "challenger": challenger.name,
            "target_id": who.id,
            "target": who.name,
            "objection": objection,
            "demand": demand,
            "round": self.round,
            "status": "open",
            "task_id": task["id"],
            "responses": [],
            "rejections": 0,
        }
        self.challenges[chid] = ch
        data = {
            "challenge_id": chid,
            **({"claim_id": claim["claim_id"]} if claim else {}),
            "challenger_id": challenger.id,
            "target_id": who.id,
            "objection": objection,
            "demand": demand,
            "round": self.round,
        }
        about = f"claim {claim['claim_id']} ({claim['statement'][:200]})" if claim else "your work"
        text = (
            f"Challenge {chid} to {about}, demanding {demand}:\n{objection}\n"
            f"Answer it with respond_to_challenge (challenge_id {chid})."
        )
        msg = await self.debate_message(
            challenger, [who], text, "challenge", data, task_id=task["id"], step_id=step_id
        )
        ch["message_id"] = msg["messageId"]
        task["history"].append(msg)
        await self._task_state(task, "input-required", who.actor, step_id)
        await self.ctx.sink.emit("debate.challenge", data, actor=challenger.actor, step_id=step_id)
        return ch, f"Opened challenge {chid} against {who.name}'s {'claim' if claim else 'work'}."

    async def respond(
        self,
        responder: MemberSpec,
        challenge_id: str,
        stance: str,
        argument: str,
        evidence: Any = None,
        *,
        step_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Answer a challenge: ``defend`` (a defence against a proof demand needs evidence, or it is
        *unsupported* and stays open), ``concede`` or ``revise``."""
        chid = str(challenge_id).strip()
        ch = self.challenges.get(chid)
        if ch is None:
            mine = ", ".join(c["challenge_id"] for c in self.open_challenges(responder.id)) or "none"
            return None, f"No challenge {chid}. Open challenges against you: {mine}."
        if ch["target_id"] != responder.id:
            return None, f"Only {ch['target']} can answer challenge {chid}."
        if ch["status"] not in OPEN_CHALLENGE:
            return None, f"Challenge {chid} is {ch['status']}: there is nothing left to answer."
        if stance not in ("defend", "concede", "revise"):
            return None, "stance must be defend, concede or revise."
        argument = conceal(responder, str(argument)).strip()[:4_000]
        ev = _evidence(evidence, responder)
        if self._seen("response", responder.id, chid, stance, argument, ev):
            return ch, "This answer is already on record."
        unsupported = stance == "defend" and ch["demand"] == "proof" and not ev
        ch["status"] = {
            "defend": "unsupported" if unsupported else "answered",
            "concede": "conceded",
            "revise": "revised",
        }[stance]
        response = {
            "responder_id": responder.id,
            "stance": stance,
            "argument": argument,
            "evidence": ev,
            "unsupported": unsupported,
        }
        ch["responses"].append(response)
        data = {"challenge_id": chid, **response}
        challenger = self.members.get(ch["challenger_id"])
        text = (
            f"Answer to challenge {chid}: {stance}"
            + (" (unsupported: a proof demand needs evidence)" if unsupported else "")
            + f"\n{argument}"
            + ("\nEvidence: " + "; ".join(_ev_text(e) for e in ev) if ev else "")
        )
        msg = await self.debate_message(
            responder,
            [challenger] if challenger else [],
            text,
            "response",
            data,
            reply_to=ch.get("message_id"),
            task_id=ch["task_id"],
            step_id=step_id,
        )
        ch["last_message_id"] = msg["messageId"]
        task = self.tasks.get(ch["task_id"])
        if task is not None:
            task["history"].append(msg)
            state = "input-required" if unsupported else ("working" if stance == "defend" else "completed")
            await self._task_state(task, state, responder.actor, step_id)
        await self.ctx.sink.emit("debate.response", data, actor=responder.actor, step_id=step_id)
        claim = self.claims.get(ch.get("claim_id") or "")
        if claim is not None:
            if stance == "concede":
                claim["conceded"] = True
                await self.set_claim_status(claim, "conceded", step_id)
            elif stance == "revise":
                claim["revision"] = argument
                await self.set_claim_status(claim, "revised", step_id)
            elif not unsupported and not claim.get("conceded"):
                await self.set_claim_status(claim, "supported", step_id)
        if unsupported:
            return ch, (
                f"Recorded, but challenge {chid} stays open as unsupported: a defence against a proof "
                "demand needs evidence (a [Source n], a URL, an artifact path or a claim id)."
            )
        return ch, f"Answer recorded on {chid} ({ch['status']})."

    async def resolve_challenge(
        self,
        challenger: MemberSpec,
        challenge_id: str,
        outcome: str,
        note: str = "",
        *,
        step_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """The challenger settles a challenge: ``accepted`` (the answer holds) or ``rejected`` (the
        claim is disputed; the target may answer once more)."""
        chid = str(challenge_id).strip()
        ch = self.challenges.get(chid)
        if ch is None:
            return None, f"No challenge {chid}."
        if ch["challenger_id"] != challenger.id:
            return None, f"Only {ch['challenger']}, who raised challenge {chid}, can resolve it."
        if ch["status"] in ("accepted", "rejected", "conceded", "revised"):
            return None, f"Challenge {chid} is already {ch['status']}."
        if outcome not in ("accepted", "rejected"):
            return None, "outcome must be accepted or rejected."
        note = conceal(challenger, str(note or "")).strip()[:2_000]
        if self._seen("resolution", challenger.id, chid, outcome, note, len(ch["responses"])):
            return ch, "This resolution is already on record."
        if outcome == "accepted":
            ch["status"], state = "accepted", "completed"
        else:
            ch["rejections"] += 1
            ch["status"] = "open" if ch["rejections"] <= 1 else "rejected"
            state = "input-required" if ch["status"] == "open" else "rejected"
        data = {"challenge_id": chid, "outcome": outcome, "note": note}
        target = self.members.get(ch["target_id"])
        text = (
            f"Challenge {chid} resolved: {outcome}."
            + (f"\n{note}" if note else "")
            + ("\nYou may answer it once more." if ch["status"] == "open" else "")
        )
        msg = await self.debate_message(
            challenger,
            [target] if target else [],
            text,
            "resolution",
            data,
            reply_to=ch.get("last_message_id") or ch.get("message_id"),
            task_id=ch["task_id"],
            step_id=step_id,
        )
        task = self.tasks.get(ch["task_id"])
        if task is not None:
            task["history"].append(msg)
            await self._task_state(task, state, challenger.actor, step_id)
        await self.ctx.sink.emit("debate.resolution", data, actor=challenger.actor, step_id=step_id)
        claim = self.claims.get(ch.get("claim_id") or "")
        if claim is not None and not claim.get("conceded"):
            await self.set_claim_status(claim, "supported" if outcome == "accepted" else "disputed", step_id)
        return ch, f"Challenge {chid} {outcome}."

    # what members see of the ledger
    def open_challenges(self, member_id: str | None = None) -> list[dict[str, Any]]:
        """Challenges waiting for their target's answer (all of them, or those against one member)."""
        return [
            c
            for c in self.challenges.values()
            if c["status"] in OPEN_CHALLENGE and (member_id is None or c["target_id"] == member_id)
        ]

    def awaiting_resolution(self, member_id: str | None = None) -> list[dict[str, Any]]:
        """Answered challenges waiting for their challenger to accept or reject the answer."""
        return [
            c
            for c in self.challenges.values()
            if c["status"] == "answered" and (member_id is None or c["challenger_id"] == member_id)
        ]

    @staticmethod
    def claim_line(claim: dict[str, Any]) -> str:
        ev = "; ".join(_ev_text(e) for e in claim.get("evidence", [])[:3])
        line = f"- {claim['claim_id']} [{claim['status']}] by {claim['author']}: {claim['statement'][:300]}"
        if claim.get("revision"):
            line += f" (revised: {claim['revision'][:300]})"
        return line + (f" (evidence: {ev[:300]})" if ev else " (no evidence given)")

    @staticmethod
    def challenge_line(ch: dict[str, Any]) -> str:
        about = f"on {ch['claim_id']}" if ch.get("claim_id") else "on their work"
        line = (
            f"- {ch['challenge_id']} [{ch['status']}] {ch['challenger']} to {ch['target']} {about}, "
            f"demanding {ch['demand']}: {ch['objection'][:400]}"
        )
        if ch["responses"]:
            last = ch["responses"][-1]
            flag = ", unsupported" if last["unsupported"] else ""
            line += f"\n  answer ({last['stance']}{flag}): {last['argument'][:300]}"
        return line

    def claims_digest(self, limit: int = CLAIM_DIGEST) -> str:
        claims = list(self.claims.values())
        lines = [self.claim_line(c) for c in claims[-limit:]]
        if len(claims) > limit:
            lines.insert(0, f"(+{len(claims) - limit} older claims)")
        return "\n".join(lines)

    def debate_text(self, member: MemberSpec) -> str:
        """The ledger as one member sees it: challenges it must answer or settle, and the claims."""
        parts: list[str] = []
        mine = self.open_challenges(member.id)
        if mine:
            parts.append(
                "\n## Challenges against your work (answer each with respond_to_challenge before you "
                "finish)\n" + "\n".join(self.challenge_line(c) for c in mine)
            )
        settle = self.awaiting_resolution(member.id)
        if settle:
            parts.append(
                "\n## Answers to your challenges (accept or reject each with resolve_challenge)\n"
                + "\n".join(self.challenge_line(c) for c in settle)
            )
        if self.claims:
            parts.append(
                "\n## Claims on record (challenge what is weak; back your own with evidence)\n"
                + self.claims_digest()
            )
        return "\n".join(parts)


def _evidence(items: Any, member: MemberSpec | None = None) -> list[dict[str, str]]:
    """Evidence items as ``{source, quote?}`` (``source``: a [Source n], a URL, an artifact path or a
    claim id)."""
    out: list[dict[str, str]] = []
    for it in items if isinstance(items, list | tuple) else []:
        if isinstance(it, str):
            source, quote = it, None
        elif isinstance(it, dict):
            source, quote = it.get("source"), it.get("quote")
        else:
            source, quote = getattr(it, "source", None), getattr(it, "quote", None)
        source = conceal(member, str(source or "")).strip()[:500]
        if not source:
            continue
        item = {"source": source}
        if quote:
            item["quote"] = conceal(member, str(quote)).strip()[:1_000]
        out.append(item)
    return out[:12]


def _ev_text(e: dict[str, str]) -> str:
    return f"{e['source']}" + (f' "{e["quote"][:160]}"' if e.get("quote") else "")


_CONFIDENCE_WORDS = {"high": 0.85, "medium": 0.6, "low": 0.35}


def _confidence(value: Any) -> float | None:
    if isinstance(value, str):
        word = _CONFIDENCE_WORDS.get(value.strip().lower())
        if word is not None:
            return word
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        v = float(value)
        return round(v / 100 if 1 < v <= 100 else v, 3) if 0 <= v <= 100 else None
    return None
