"""The council's debate: how its members argue and conclude together (design doc, section 2).

``plan → devil's advocate → execute → deliberate → converge → compile → co-sign``

* **Devil's advocate.** Right after the plan, one debater is picked at random in code
  (``secrets.SystemRandom``): never an overseer, never the chair unless it is the only debater. The
  pick is a secret only the run's owner sees (the durable ``council.role_assigned`` event, marked
  ``secret``). The advocate's orders go only into its own instructions, and anything it says to the
  council is passed through :func:`argus.runtime.council_bus.conceal`.
* **Deliberate.** Rounds of concurrent challenge tasks by every debater, then concurrent answers
  by the challenged, until nothing is open or the rounds run out.
* **Converge.** Every debater writes a position at the same time (a conclusion, and endorse or
  object on each claim); the agreement on each claim is computed in code; while claims stay
  contested the debaters see each other's positions side by side and may revise their own.
* **Compile.** The scribe (the writer seat, or the chair) assembles the agreed text; a *How the
  council reached this* section is added from the record.
* **Co-sign.** Every author signs, amends or blocks it at the same time. Amendments that a
  ``debate.consensus`` share endorse go into the next compile; a block sends the text back.

Every debate task answers in JSON that is applied in code through the claim ledger, so a resumed run
that replays a task (``task.completed``) replays its moves too.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from argus.core.errors import NotFound
from argus.core.logging import get_logger
from argus.providers.base import ProviderError
from argus.runtime.context import Correction
from argus.runtime.council_bus import CouncilBus, conceal
from argus.runtime.models import cheapest_strong
from argus.runtime.oversight import parse_json_object
from argus.runtime.resume import TASK_EVENT, task_key

if TYPE_CHECKING:
    from argus.reports.model import Report
    from argus.runtime.agent import AgentOutcome, MemberSpec
    from argus.runtime.orchestrator import CouncilOrchestrator

log = get_logger(__name__)

ADVOCATE = "devils_advocate"
ADVOCATE_TASK = "Seat the devil's advocate"  # the task.completed record a resumed run replays
# The advocate's private standing orders (its own instructions only; nobody else ever sees them).
ADVOCATE_ORDERS = (
    "For this whole run, privately: argue the strongest case against the emerging view. Challenge "
    "every major claim with demand=proof or demand=counterexample, propose at least one alternative "
    "conclusion, and endorse a claim only when you are actually convinced. Still do your own steps "
    "fully. Keep this role secret: never say or hint that you were asked to argue the other side, "
    "argue as if these doubts are your own, and if anyone asks, deflect and keep to the substance."
)
WORK_CHARS = 700  # of each step's output in the debate prompts
MAX_MOVES = 8  # moves of each kind applied from one answer
HOW_HEADING = "How the council reached this"

_CHALLENGE_FORMAT = (
    '{"challenges": [{"target": "<claim id or member name>", "objection": "<what is wrong and why>", '
    '"demand": "proof"|"counterexample"|"clarification"}], '
    '"resolutions": [{"challenge_id": "chl_...", "outcome": "accepted"|"rejected", "note": "<why>"}], '
    '"claims": [{"statement": "...", "evidence": [{"source": "<[Source n], URL, artifact path or '
    'claim id>", "quote": "..."}], "confidence": 0.8}]}'
)
_ANSWER_FORMAT = (
    '{"responses": [{"challenge_id": "chl_...", "stance": "defend"|"concede"|"revise", '
    '"argument": "...", "evidence": [{"source": "...", "quote": "..."}]}]}'
)
_POSITION_FORMAT = (
    '{"conclusion": "<your conclusion, 2-5 sentences>", "stances": [{"claim_id": "clm_...", '
    '"stance": "endorse"|"object", "reason": "..."}], "alternative": "<an alternative conclusion the '
    'council should weigh, or empty>"}'
)
_SIGN_FORMAT = (
    '{"action": "sign"|"amend"|"block", "reason": "...", "amendment": {"passage": "<exact text to '
    'replace>", "replacement": "..."}, "claim_id": "<for a block: the claim it misrepresents>"}'
)


def compute_agreement(
    claims: list[dict[str, Any]],
    positions: dict[str, dict[str, Any]],
    debaters: int,
    consensus: float,
    open_claims: set[str],
) -> list[dict[str, Any]]:
    """The agreement on each claim, from the debaters' latest positions.

    * ``agreed``: at least a ``consensus`` share of the debaters endorse it and no challenge on it is
      open
    * ``rejected``: it was conceded, or most debaters object to it
    * ``contested``: anything else (endorsements and objections, neither side at the threshold)
    """
    out = []
    for c in claims:
        cid = c["claim_id"]
        endorse, objects = [], []
        for mid, p in positions.items():
            stance = next((s["stance"] for s in p.get("stances", []) if s["claim_id"] == cid), None)
            if stance == "endorse":
                endorse.append(mid)
            elif stance == "object":
                objects.append(mid)
        if c.get("conceded"):
            agreement = "rejected"
        elif debaters and len(endorse) >= consensus * debaters - 1e-9 and cid not in open_claims:
            agreement = "agreed"
        elif len(objects) * 2 > debaters:
            agreement = "rejected"
        else:
            agreement = "contested"
        out.append({"claim_id": cid, "agreement": agreement, "endorse": endorse, "object": objects})
    return out


def _items(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)][:MAX_MOVES] if isinstance(value, list) else []


def _cell(text: str, n: int = 140) -> str:
    return " ".join(str(text).split())[:n].replace("|", "/")


class Debate:
    def __init__(self, orch: CouncilOrchestrator) -> None:
        self.o = orch
        self.ctx = orch.ctx
        self.bus: CouncilBus = orch.council
        self.positions: dict[str, dict[str, Any]] = {}  # each debater's latest position
        self.rounds: list[dict[str, dict[str, Any]]] = []  # the positions of each converge round
        self.agreement: list[dict[str, Any]] = []
        self.signatures: list[dict[str, Any]] = []
        self.amendments: list[dict[str, Any]] = []
        self._restored = False
        self._light: dict[str, MemberSpec] = {}  # each debater's seat for light moves
        self._seen: dict[str, str] = {}  # what each debater was shown at its last challenge
        self._costs: list[dict[str, Any]] = []

    # ------------------------------------------------------------ who debates
    @property
    def policy(self) -> Any:
        return self.ctx.engine.rails.debate

    @property
    def debaters(self) -> list[MemberSpec]:
        return [m for m in self.o.members if m.debater and not m.ephemeral]

    @property
    def active(self) -> bool:
        """The council debates its conclusion (at least two debaters); otherwise the run keeps one
        synthesis and a majority vote."""
        return self.bus.debating and len(self.debaters) >= 2

    def _name(self, member_id: str) -> str:
        m = self.bus.members.get(member_id) or next((x for x in self.o.members if x.id == member_id), None)
        return m.name if m else member_id

    async def _round(self, phase: str, n: int, status: str) -> None:
        if status == "finished":
            # Between debate rounds, as between any actions: cancel, pause and the run's limits
            # (a round of concurrent tasks may have spent past the budget) apply.
            await self.ctx.checkpoint()
        await self.ctx.sink.emit(
            "debate.round",
            {
                "phase": phase,
                "round": n,
                "status": status,
                "open_challenges": len(self.bus.open_challenges()),
            },
        )

    async def _each(
        self, members: list[MemberSpec], work: Callable[[MemberSpec], Awaitable[Any]]
    ) -> list[Any]:
        """Run ``work`` for every member at the same time (within the run's parallelism). A member
        whose provider fails gives no answer; a cancelled or halted run stops them all."""
        sem = asyncio.Semaphore(self.ctx.engine.rails.max_parallel_agents)

        async def one(m: MemberSpec) -> Any:
            async with sem:
                try:
                    return await work(m)
                except (ProviderError, NotFound) as exc:
                    log.warning("debate.task_failed", member=m.name, error=str(exc)[:200])
                    return None

        tasks = [asyncio.create_task(one(m), name=f"debate-{m.id}") for m in members]
        try:
            return list(await asyncio.gather(*tasks))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ask(
        self,
        member: MemberSpec,
        prompt: str,
        title: str,
        phase: str,
        context: str = "",
        *,
        light: bool = True,
    ) -> tuple[AgentOutcome, dict[str, Any]]:
        """One debate task. A *light* move (challenge, position, signature, amendment vote) reads the
        digests it is given: one call without tools, on the seat's light model. An answer to a
        challenge (``light=False``) keeps the seat's model and the read-only tools, to fetch proof."""
        if light:
            seat = await self.light_seat(member)
            out = await self.o.runner(seat).run_task(
                prompt, step_title=title, phase=phase, context=context, tools="none"
            )
        else:
            out = await self.o.runner(member).run_task(
                prompt, step_title=title, phase=phase, context=context, tools="read"
            )
        return out, (parse_json_object(out.text or "") or {}) if out.ok else {}

    # ------------------------------------------------------------ cost
    async def light_seat(self, member: MemberSpec) -> MemberSpec:
        """``member`` on the newest cheap model of its own provider that is still a strong reasoner
        (``debate.light_min_reasoning``), with its own model as the fallback. The same member in every
        other way (id, persona, stance), so the debate reads exactly as before. CLI seats, and seats
        already on the cheapest qualifying model, keep their own."""
        if member.id in self._light:
            return self._light[member.id]
        seat = member
        if self.policy.light_model == "latest_cheapest":
            seat = await cheapest_strong(self.ctx, member, self.policy.light_min_reasoning)
        self._light[member.id] = seat
        return seat

    def _usage(self) -> tuple[float, int]:
        used = self.ctx.engine.usage
        return used.cost_usd, used.input_tokens + used.output_tokens

    async def _cost(self, phase: str, before: tuple[float, int], tasks: int) -> None:
        """What a phase of the debate cost, and on which models (``debate.cost``)."""
        usd, tokens = self._usage()
        entry = {
            "phase": phase,
            "usd": round(max(usd - before[0], 0.0), 6),
            "tokens": max(tokens - before[1], 0),
            "tasks": tasks,
            "light_models": {mid: s.model for mid, s in self._light.items()},
        }
        self._costs.append(entry)
        await self.ctx.sink.emit("debate.cost", entry)

    # ------------------------------------------------------------ devil's advocate
    async def seat_devils_advocate(self) -> MemberSpec | None:
        """Pick one debater at random, in code, as the secret devil's advocate (the same one again in
        a resumed run). Only the owner is told, by a durable event marked ``secret``."""
        if not self.bus.debating:
            return None
        if not self.policy.devils_advocate:
            await self._role_skipped("the devil's advocate is turned off (debate.devils_advocate)")
            return None
        debaters = self.debaters
        if len(debaters) < 2:
            await self._role_skipped("the council needs at least two debaters for a devil's advocate")
            return None
        pool = [m for m in debaters if m.id != self.o.chair.id] or debaters
        key = task_key("devils_advocate", None, ADVOCATE_TASK, self.o.chair.id)
        resume = self.ctx.extra.get("resume")
        earlier = resume.take(key) if resume is not None else None
        pick = next((m for m in pool if m.id == earlier), None)
        replayed = pick is not None
        if pick is None:
            pick = secrets.SystemRandom().choice(pool)
        pick.stance = ADVOCATE
        self.ctx.controls.standing[pick.id].append(
            Correction(f"cor_{uuid.uuid4().hex[:8]}", ADVOCATE_ORDERS, "member", "system", pick.id)
        )
        await self.ctx.sink.emit(
            "council.role_assigned",
            {
                "member_id": pick.id,
                "name": pick.name,
                "stance": ADVOCATE,
                "pool": [m.id for m in pool],
                "secret": True,
                **({"replayed": True} if replayed else {}),
            },
        )
        await self.ctx.sink.emit(
            TASK_EVENT,
            {
                "key": key,
                "phase": "devils_advocate",
                "step_title": ADVOCATE_TASK,
                "member_id": self.o.chair.id,
                "ok": True,
                "reason": "replayed" if replayed else "done",
                "text": pick.id,  # inline, so a resumed run seats the same advocate
                **({"replayed": True} if replayed else {}),
            },
        )
        return pick

    async def _role_skipped(self, reason: str) -> None:
        await self.ctx.sink.emit("council.role_assigned", {"skipped": True, "reason": reason, "secret": True})

    # ------------------------------------------------------------ resume
    async def restore(self) -> None:
        """A resumed run starts the debate from the moves the old run made while it worked (tool
        calls inside its steps, which a replayed step does not make again). The debate tasks' own
        moves come back with their replayed answers. Ids follow from content, so a move made again
        by a step that runs for real is not recorded twice."""
        resume = self.ctx.extra.get("resume")
        if resume is None or self._restored:
            return
        self._restored = True
        from argus.db.models import RunEvent

        kinds = ("debate.claim", "debate.challenge", "debate.response", "debate.resolution", "debate.round")
        try:
            async with self.ctx.sessionmaker() as s:
                rows = (
                    await s.execute(
                        select(RunEvent.payload)
                        .where(RunEvent.run_id == resume.from_run_id, RunEvent.type.in_(kinds))
                        .order_by(RunEvent.seq)
                    )
                ).all()
        except Exception as exc:  # the old ledger helps; it is never a dependency of the new run
            log.warning("debate.restore_failed", error=str(exc)[:200])
            return
        members = self.bus.members
        for (payload,) in rows:
            d = payload.get("data") or {}
            kind = payload.get("type")
            if kind == "debate.round":
                break  # the debate itself began: its tasks are replayed with their moves
            actor = members.get((payload.get("actor") or {}).get("member_id") or "")
            if kind == "debate.claim" and (who := members.get(d.get("author_id", ""))):
                await self.bus.make_claim(who, d.get("statement", ""), d.get("evidence"), d.get("confidence"))
            elif kind == "debate.challenge" and (who := members.get(d.get("challenger_id", ""))):
                target = d.get("claim_id") or d.get("target_id") or ""
                await self.bus.open_challenge(who, target, d.get("objection", ""), d.get("demand", "proof"))
            elif kind == "debate.response" and (who := members.get(d.get("responder_id", ""))):
                await self.bus.respond(
                    who,
                    d.get("challenge_id", ""),
                    d.get("stance", ""),
                    d.get("argument", ""),
                    d.get("evidence"),
                )
            elif kind == "debate.resolution" and actor is not None:
                await self.bus.resolve_challenge(
                    actor, d.get("challenge_id", ""), d.get("outcome", ""), d.get("note", "")
                )

    # ------------------------------------------------------------ the record, as prompts show it
    def work_text(self) -> str:
        return "\n".join(
            f"- {s.title} ({s.assignee.name}, {s.status}): {' '.join(s.output.split())[:WORK_CHARS]}"
            for s in self.o.steps
            if s.output
        )

    def key_claims(self) -> list[dict[str, Any]]:
        return list(self.bus.claims.values())[:20]

    def _positions_text(self, positions: dict[str, dict[str, Any]], exclude: str | None = None) -> str:
        blocks = []
        for mid, p in positions.items():
            if mid == exclude:
                continue
            lines = [f"### {self._name(mid)}", f"Conclusion: {p['conclusion'][:800]}"]
            lines += [f"- {s['claim_id']}: {s['stance']}: {s['reason'][:240]}" for s in p.get("stances", [])]
            if p.get("alternative"):
                lines.append(f"Alternative: {p['alternative'][:400]}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    async def apply(self, member: MemberSpec, data: dict[str, Any]) -> None:
        """Apply the moves of one debate answer through the claim ledger."""
        bus = self.bus
        for c in _items(data.get("claims")):
            if str(c.get("statement") or "").strip():
                await bus.make_claim(member, str(c["statement"]), c.get("evidence"), c.get("confidence"))
        for c in _items(data.get("challenges")):
            target = str(c.get("target") or c.get("claim_id") or "")
            await bus.open_challenge(
                member, target, str(c.get("objection") or ""), str(c.get("demand") or "proof").lower()
            )
        for c in _items(data.get("responses")):
            await bus.respond(
                member,
                str(c.get("challenge_id") or ""),
                str(c.get("stance") or "defend").lower(),
                str(c.get("argument") or ""),
                c.get("evidence"),
            )
        for c in _items(data.get("resolutions")):
            await bus.resolve_challenge(
                member,
                str(c.get("challenge_id") or ""),
                str(c.get("outcome") or "").lower(),
                str(c.get("note") or ""),
            )

    # ------------------------------------------------------------ deliberate
    async def deliberate(self) -> None:
        """Rounds of challenges (every debater, concurrently) and answers (every challenged member,
        concurrently), until nothing is open or ``debate.rounds`` run out."""
        await self.restore()
        before, tasks = self._usage(), 0
        for r in range(1, self.policy.rounds + 1):
            if r > 1 and not (self.bus.open_challenges() or self.bus.awaiting_resolution()):
                break
            self.bus.round = r
            await self._round("deliberate", r, "started")
            # A debater shown exactly what it was shown last round would only repeat itself.
            examiners = [m for m in self.debaters if r == 1 or self._view(m) != self._seen.get(m.id)]
            await self._each(examiners, functools.partial(self._challenge, r=r))
            targets = [m for m in self.debaters if self.bus.open_challenges(m.id)]
            if targets:
                await self._each(targets, functools.partial(self._answer, r=r))
            tasks += len(examiners) + len(targets)
            await self._round("deliberate", r, "finished")
        await self._cost("deliberate", before, tasks)

    def _view(self, m: MemberSpec) -> str:
        """A fingerprint of what a challenge task shows ``m``: claims, work, open challenges and the
        answers awaiting its resolution."""
        bus = self.bus
        seen = [
            bus.claims_digest(),
            self.work_text(),
            [bus.challenge_line(c) for c in bus.open_challenges()],
            [bus.challenge_line(c) for c in bus.awaiting_resolution(m.id)],
        ]
        return hashlib.sha256(repr(seen).encode()).hexdigest()

    async def _challenge(self, m: MemberSpec, r: int) -> None:
        bus = self.bus
        self._seen[m.id] = self._view(m)
        settle = bus.awaiting_resolution(m.id)
        prompt = (
            f"Deliberation, round {r}. Challenge what is weak, unsupported or wrong in your teammates' "
            "claims and work (never your own), and settle the answers to challenges you raised.\n\n"
            f"## Claims on record\n{bus.claims_digest() or '(none yet)'}\n\n"
            f"## The council's work\n{self.work_text() or '(none)'}\n\n"
            "## Open challenges\n"
            + ("\n".join(bus.challenge_line(c) for c in bus.open_challenges()) or "(none)")
            + (
                "\n\n## Answers to your challenges (accept or reject each)\n"
                + "\n".join(bus.challenge_line(c) for c in settle)
                if settle
                else ""
            )
            + f"\n\nRespond with ONLY a JSON object: {_CHALLENGE_FORMAT}. An empty list means nothing "
            "needs challenging. Challenge only what matters, and say why."
        )
        if m.stance == ADVOCATE:
            prompt += "\nThis round, raise at least one challenge."
        _, data = await self._ask(m, prompt, f"Deliberation round {r}: challenge", "deliberate_challenge")
        await self.apply(m, data)

    async def _answer(self, m: MemberSpec, r: int) -> None:
        bus = self.bus
        mine = bus.open_challenges(m.id)
        if not mine:
            return
        prompt = (
            f"Deliberation, round {r}: teammates have challenged your work. Answer each challenge below: "
            "defend it with evidence, concede it, or revise your claim. A defence against a proof demand "
            "without evidence is recorded as unsupported and stays open.\n\n"
            "## Challenges against you\n"
            + "\n".join(bus.challenge_line(c) for c in mine)
            + f"\n\n## Claims on record\n{bus.claims_digest() or '(none)'}"
            + f"\n\nRespond with ONLY a JSON object: {_ANSWER_FORMAT}"
        )
        _, data = await self._ask(
            m,
            prompt,
            f"Deliberation round {r}: answer",
            "deliberate_answer",
            context=bus._own_work(m),
            light=False,
        )
        await self.apply(m, data)

    # ------------------------------------------------------------ converge
    async def converge(self) -> list[dict[str, Any]]:
        """Positions from every debater at once, the agreement computed in code, and more rounds of
        positions while claims stay contested (``debate.converge_rounds``)."""
        await self.restore()
        before, tasks = self._usage(), 0
        r = 0
        for r in range(1, self.policy.converge_rounds + 2):
            await self._round("converge", r, "started")
            claims = self.key_claims()
            previous = dict(self.positions)
            results = await self._each(
                self.debaters, functools.partial(self._position, r=r, claims=claims, previous=previous)
            )
            tasks += len(self.debaters)
            fresh = {p["member_id"]: p for p in results if p}
            self.positions.update(fresh)
            self.rounds.append(fresh)
            self.agreement = self._agree(claims)
            for a in self.agreement:
                await self.bus.set_claim_status(self.bus.claims[a["claim_id"]], a["agreement"])
            await self._round("converge", r, "finished")
            if not any(a["agreement"] == "contested" for a in self.agreement):
                break
        await self.ctx.sink.emit(
            "debate.consensus",
            {
                "round": r,
                "phase": "converge",
                "claims": self.agreement,
                "signatures": [],
                "quorum_met": None,
                "outcome": None,
            },
        )
        await self._cost("converge", before, tasks)
        return self.agreement

    def _agree(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        debaters = self.debaters
        ids = {m.id for m in debaters}
        positions = {mid: p for mid, p in self.positions.items() if mid in ids}
        open_claims = {c["claim_id"] for c in self.bus.open_challenges() if c.get("claim_id")}
        return compute_agreement(claims, positions, len(debaters), self.policy.consensus, open_claims)

    async def _position(
        self,
        m: MemberSpec,
        r: int,
        claims: list[dict[str, Any]],
        previous: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        bus = self.bus
        others = self._positions_text(previous, exclude=m.id)
        prompt = (
            f"Converge, round {r}: write your position on the council's conclusion. Take a stance on "
            "every claim below: endorse it if the evidence and the debate support it, object if not, "
            "with a reason.\n\n## Claims on record\n"
            + ("\n".join(bus.claim_line(c) for c in claims) or "(none)")
            + (
                "\n\n## The debate\n"
                + "\n".join(bus.challenge_line(c) for c in list(bus.challenges.values())[:12])
                if bus.challenges
                else ""
            )
            + (
                "\n\n## Your teammates' positions so far (read them side by side; revise yours if they "
                f"persuade you)\n{others}"
                if others
                else ""
            )
            + (
                f"\n\n## Your previous position\n{self._positions_text({m.id: previous[m.id]})}"
                if m.id in previous
                else ""
            )
            + f"\n\nRespond with ONLY a JSON object: {_POSITION_FORMAT}"
        )
        out, data = await self._ask(m, prompt, f"Position, round {r}", "position", context=self.work_text())
        if not out.ok or not (out.text or "").strip():
            return None
        known = {c["claim_id"] for c in claims}
        stances = []
        for s in _items(data.get("stances")) if data else []:
            stance = str(s.get("stance") or "").lower()
            if s.get("claim_id") in known and stance in ("endorse", "object"):
                reason = conceal(m, str(s.get("reason") or ""))[:1_000]
                stances.append({"claim_id": s["claim_id"], "stance": stance, "reason": reason})
        conclusion = conceal(m, str(data.get("conclusion") or out.text if data else out.text).strip())[:3_000]
        alternative = conceal(m, str(data.get("alternative") or "").strip())[:1_500] if data else ""
        position = {
            "member_id": m.id,
            "round": r,
            "conclusion": conclusion,
            "stances": stances,
            **({"alternative": alternative} if alternative else {}),
        }
        event = {k: position[k] for k in ("member_id", "round", "conclusion", "stances")}
        text = f"Position, round {r}: {conclusion}" + "".join(
            f"\n- {s['claim_id']}: {s['stance']} ({s['reason'][:200]})" for s in stances
        )
        await bus.debate_message(m, [], text, "position", position, audience="council")
        await self.ctx.sink.emit("debate.position", event, actor=m.actor)
        return position

    # ------------------------------------------------------------ compile
    async def compile(self, n: int, amendments: list[dict[str, Any]], problems: str) -> str:
        """The scribe assembles the agreed text; the record of how it was reached is added after."""
        await self._round("compile", n, "started")
        scribe = self.o.writer
        fmt = ", ".join(self.ctx.outputs) or "markdown"
        prompt = (
            f"Compile the council's conclusion into the final deliverable for the user, in Markdown (it "
            f"will be exported to: {fmt}). You are the scribe: assemble what the council agreed, in the "
            "authors' own words from their positions, and keep citations. Present contested claims "
            "with both sides and their evidence, and record dissent by name. Do not add content the "
            "council did not agree on; a new factual statement must go on record with make_claim "
            "instead.\n\n" + self.record_text()
        )
        if amendments:
            prompt += "\n\n## Amendments the council endorsed (apply each)\n" + "\n".join(
                f'- Replace "{a["passage"][:600]}" with "{a["replacement"][:900]}" ({a["reason"][:200]})'
                for a in amendments
            )
        if problems:
            prompt += f"\n\n## Problems raised when co-signing (fix each)\n{problems[:4_000]}"
        prompt += (
            "\n\nReturn ONLY the deliverable. Do not write a section on how the council reached it: "
            "that section is added after yours."
        )
        out = await self.o.runner(scribe).run_task(
            prompt, step_title=f"Compile, round {n}", phase="compile", context=self.work_text()
        )
        text = (out.text or "").strip() if out.ok else ""
        if not text:
            text = (await self.o.synthesise()).strip()
        await self._round("compile", n, "finished")
        return f"{text}\n\n{self.how_section()}"

    def record_text(self) -> str:
        """What the council agreed and disputed, for the scribe (names only, never a stance)."""
        claims = self.bus.claims
        groups: dict[str, list[str]] = {"agreed": [], "contested": [], "rejected": []}
        for a in self.agreement:
            c = claims.get(a["claim_id"])
            if c is None:
                continue
            line = self.bus.claim_line(c)
            if a["endorse"]:
                line += f" (endorsed by {', '.join(self._name(i) for i in a['endorse'])})"
            if a["object"]:
                reasons = "; ".join(f"{self._name(i)}: {self._reason(i, c['claim_id'])}" for i in a["object"])
                line += f" (objections: {reasons[:600]})"
            groups[a["agreement"]].append(line)
        return (
            "## Agreed claims\n"
            + ("\n".join(groups["agreed"]) or "(none)")
            + "\n\n## Contested claims (give both sides and their evidence)\n"
            + ("\n".join(groups["contested"]) or "(none)")
            + "\n\n## Rejected claims (leave them out)\n"
            + ("\n".join(groups["rejected"]) or "(none)")
            + "\n\n## The authors' positions\n"
            + (self._positions_text(self.positions) or "(none)")
            + "\n\n## Dissent\n"
            + ("\n".join(f"- {d}" for d in self.dissent()) or "(none)")
        )

    def _reason(self, member_id: str, claim_id: str) -> str:
        p = self.positions.get(member_id) or {}
        return next((s["reason"] for s in p.get("stances", []) if s["claim_id"] == claim_id), "")[:300]

    def dissent(self) -> list[str]:
        """Objections that did not carry, alternatives, and challenges left open (by name only)."""
        out: list[str] = []
        claims = self.bus.claims
        for a in self.agreement:
            if a["agreement"] == "rejected":
                continue
            c = claims.get(a["claim_id"])
            for mid in a["object"]:
                reason = self._reason(mid, a["claim_id"])
                out.append(
                    f"{self._name(mid)} objects to {a['claim_id']}"
                    + (f' ("{c["statement"][:160]}")' if c else "")
                    + (f": {reason}" if reason else "")
                )
        for mid, p in self.positions.items():
            if p.get("alternative"):
                out.append(f"{self._name(mid)} proposes an alternative conclusion: {p['alternative'][:400]}")
        for ch in self.bus.challenges.values():
            if ch["status"] in ("open", "unsupported", "rejected"):
                out.append(
                    f"{ch['challenger']}'s challenge {ch['challenge_id']} to {ch['target']} is unresolved "
                    f"({ch['status']}): {' '.join(ch['objection'].split())[:240]}"
                )
        return out

    def how_section(self, heading: str = "##") -> str:
        """The *How the council reached this* section, from the record."""
        bus = self.bus
        authors = ", ".join(f"{m.name} ({m.role})" for m in self.debaters)
        lines = [f"{heading} {HOW_HEADING}", "", f"**Authors:** {authors}.", ""]
        challenges = list(bus.challenges.values())
        answered = sum(1 for c in challenges if c["responses"])
        lines.append(
            f"The council put {len(bus.claims)} claim{'s' if len(bus.claims) != 1 else ''} on record and "
            f"raised {len(challenges)} challenge{'s' if len(challenges) != 1 else ''} "
            f"({answered} answered) over {len(self.rounds)} round{'s' if len(self.rounds) != 1 else ''} "
            "of positions."
        )
        if self.positions:
            lines += ["", "**Positions**", ""]
            lines += [
                f"- **{self._name(mid)}:** {_cell(p['conclusion'], 400)}" for mid, p in self.positions.items()
            ]
        if self.agreement:
            lines += [
                "",
                "**Agreement on each claim**",
                "",
                "| Claim | Agreement | Endorsed by | Objected by |",
            ]
            lines.append("| --- | --- | --- | --- |")
            for a in self.agreement:
                c = bus.claims.get(a["claim_id"]) or {}
                lines.append(
                    f"| {a['claim_id']}: {_cell(c.get('statement', ''))} | {a['agreement']} | "
                    f"{', '.join(self._name(i) for i in a['endorse']) or '-'} | "
                    f"{', '.join(self._name(i) for i in a['object']) or '-'} |"
                )
        dissent = self.dissent()
        lines += ["", "**Dissent**", ""]
        lines += [f"- {_cell(d, 500)}" for d in dissent] or ["- None recorded."]
        return "\n".join(lines)

    # ------------------------------------------------------------ co-sign
    async def cosign(self, text: str, n: int, *, final: bool) -> tuple[str, str, list[dict[str, Any]]]:
        """Every author signs, amends or blocks the text at the same time. Returns (outcome,
        problems for the next compile, the amendments the council endorsed)."""
        await self._round("cosign", n, "started")
        debaters = self.debaters
        quorum = len(debaters) // 2 + 1
        vote_id = f"vote_{uuid.uuid4().hex[:10]}"
        # Co-signing replaces the vote; the vote events stay for the views that show them.
        await self.ctx.sink.emit(
            "vote.opened",
            {
                "vote_id": vote_id,
                "motion": "Co-sign the council's conclusion?",
                "options": [
                    {"id": "sign", "label": "Sign"},
                    {"id": "amend", "label": "Amend"},
                    {"id": "block", "label": "Block"},
                ],
                "quorum": quorum,
            },
            actor=self.o.chair.actor,
        )
        results = await self._each(debaters, functools.partial(self._sign, n=n, text=text, vote_id=vote_id))
        sigs = [s for s in results if s]
        proposed = [s for s in sigs if s["action"] == "amend" and s.get("amendment")]
        endorsed = await self._weigh(proposed, n) if proposed else []
        signers = [s for s in sigs if s["action"] == "sign"]
        blocks = [s for s in sigs if s["action"] == "block"]
        quorum_met = len(signers) >= quorum
        if blocks or endorsed or not quorum_met:
            outcome = "unsigned" if final else "revise"
        else:
            outcome = "signed"
        self.signatures = sigs
        tally = {a: sum(1 for s in sigs if s["action"] == a) for a in ("sign", "amend", "block")}
        await self.ctx.sink.emit(
            "vote.closed",
            {
                "vote_id": vote_id,
                "result_option_id": "sign" if outcome == "signed" else "revise",
                "tally": tally,
            },
            actor=self.o.chair.actor,
        )
        await self.ctx.sink.emit(
            "debate.consensus",
            {
                "round": n,
                "phase": "cosign",
                "claims": self.agreement,
                "signatures": [
                    {k: v for k, v in s.items() if k in ("member_id", "action", "reason", "amendment")}
                    for s in sigs
                ],
                "quorum_met": quorum_met,
                "outcome": outcome,
            },
        )
        await self._round("cosign", n, "finished")
        problems = "\n".join(
            [f"[{self._name(s['member_id'])}] block: {s['reason']}" for s in blocks]
            + [
                f"[{self._name(s['member_id'])}] amend: {s['reason']}"
                for s in sigs
                if s["action"] == "amend" and not s.get("amendment")
            ]
        )
        return outcome, problems, endorsed

    async def _sign(self, m: MemberSpec, n: int, text: str, vote_id: str) -> dict[str, Any] | None:
        prompt = (
            "Co-sign the council's deliverable below. You are one of its authors. Do one of three "
            "things: sign it if it faithfully states what the council agreed; amend it, giving the exact "
            "passage to replace, the replacement and why; or block it, naming the claim it "
            f"misrepresents. Respond with ONLY a JSON object: {_SIGN_FORMAT}."
            f"\n\nDELIVERABLE:\n{text[:30_000]}"
        )
        out, data = await self._ask(m, prompt, f"Co-sign, round {n}", "cosign")
        if not out.ok:
            return None
        action = str(data.get("action") or "sign").lower()
        action = action if action in ("sign", "amend", "block") else "sign"
        sig: dict[str, Any] = {
            "member_id": m.id,
            "action": action,
            "reason": conceal(m, str(data.get("reason") or "")).strip()[:1_500],
        }
        amendment = data.get("amendment") if isinstance(data.get("amendment"), dict) else None
        if action == "amend" and amendment and str(amendment.get("passage") or "").strip():
            sig["amendment"] = {
                "passage": conceal(m, str(amendment["passage"]))[:2_000],
                "replacement": conceal(m, str(amendment.get("replacement") or ""))[:4_000],
            }
        if action == "block" and data.get("claim_id"):
            sig["claim_id"] = str(data["claim_id"])[:40]
        await self.ctx.sink.emit(
            "vote.cast",
            {"vote_id": vote_id, "member_id": m.id, "option_id": action, "rationale": sig["reason"]},
            actor=m.actor,
        )
        body = f"{action.title()}: {sig['reason']}" + (
            f'\nReplace "{sig["amendment"]["passage"][:300]}" with "{sig["amendment"]["replacement"][:300]}"'
            if sig.get("amendment")
            else ""
        )
        await self.bus.debate_message(m, [], body, "signature", sig, audience="council")
        return sig

    async def _weigh(self, proposed: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
        """The authors endorse amendments, concurrently; those a consensus share endorse are applied
        in the next compile, the others are recorded."""
        amendments = [
            {
                "id": f"am_{i}",
                "member_id": s["member_id"],
                "passage": s["amendment"]["passage"],
                "replacement": s["amendment"]["replacement"],
                "reason": s["reason"],
                "endorsed_by": [s["member_id"]],
            }
            for i, s in enumerate(proposed, 1)
        ]
        listing = "\n".join(
            f'- {a["id"]} by {self._name(a["member_id"])}: replace "{a["passage"][:500]}" with '
            f'"{a["replacement"][:700]}" ({a["reason"][:200]})'
            for a in amendments
        )

        async def weigh(m: MemberSpec) -> tuple[str, list[str]]:
            prompt = (
                "Your co-authors proposed these amendments to the council's deliverable. Endorse each "
                "one that should be applied. Respond with ONLY a JSON object: "
                '{"endorse": ["am_1"], "reasons": {"am_1": "..."}}'
                f"\n\n## Amendments\n{listing}"
            )
            _, data = await self._ask(m, prompt, f"Co-sign, round {n}: amendments", "cosign")
            chosen = data.get("endorse") if isinstance(data.get("endorse"), list) else []
            return m.id, [str(x) for x in chosen]

        results = await self._each(self.debaters, weigh)
        for r in results:
            if not r:
                continue
            mid, chosen = r
            for a in amendments:
                if a["id"] in chosen and mid not in a["endorsed_by"]:
                    a["endorsed_by"].append(mid)
        need = self.policy.consensus * len(self.debaters) - 1e-9
        for a in amendments:
            a["applied"] = len(a["endorsed_by"]) >= need
        self.amendments += amendments
        return [a for a in amendments if a["applied"]]

    # ------------------------------------------------------------ the whole conclusion
    async def conclude(self, max_revisions: int) -> str:
        """Deliberate, converge, compile and co-sign; returns the deliverable with its byline."""
        await self.deliberate()
        await self.converge()
        before = self._usage()
        text = await self.compile(1, [], "")
        tasks = 1
        rounds = max_revisions + 1
        for n in range(1, rounds + 1):
            outcome, problems, endorsed = await self.cosign(text, n, final=n == rounds)
            tasks += len(self.debaters)
            if outcome != "revise":
                break
            text = await self.compile(n + 1, endorsed, problems)
            tasks += 1
        await self._cost("compile_cosign", before, tasks)
        return f"{text}\n\n{self.byline()}"

    def byline(self) -> str:
        authors = ", ".join(m.name for m in self.debaters)
        signed = [self._name(s["member_id"]) for s in self.signatures if s["action"] == "sign"]
        others = [
            f"{self._name(s['member_id'])} ({s['action']})" for s in self.signatures if s["action"] != "sign"
        ]
        line = f"*Co-authored by the council: {authors}. Signed by: {', '.join(signed) or 'nobody'}.*"
        return "---\n\n" + line + (f"\n*Not signed: {', '.join(others)}.*" if others else "")

    def _statement(self, claim_id: str) -> str:
        return _cell((self.bus.claims.get(claim_id) or {}).get("statement", ""), 200)

    def annotate_report(self, report: Report) -> None:
        """Report mode: the byline and the *How the council reached this* appendix."""
        from argus.reports.model import Section

        bus = self.bus
        report.author = f"Argus Council: {', '.join(m.name for m in self.debaters)}"[:400]
        challenges = list(bus.challenges.values())
        blocks: list[dict[str, Any]] = [
            {
                "type": "paragraph",
                "text": f"Authors: {', '.join(f'{m.name} ({m.role})' for m in self.debaters)}. The council "
                f"put {len(bus.claims)} claims on record, raised {len(challenges)} challenges and wrote "
                f"{len(self.rounds)} round(s) of positions before co-signing this report.",
            }
        ]
        if self.positions:
            blocks.append(
                {
                    "type": "bullets",
                    "items": [
                        f"{self._name(mid)}: {_cell(p['conclusion'], 400)}"
                        for mid, p in self.positions.items()
                    ],
                }
            )
        if self.agreement:
            blocks.append(
                {
                    "type": "bullets",
                    "items": [
                        f"{a['claim_id']}: {self._statement(a['claim_id'])} ({a['agreement']}; endorsed by "
                        f"{', '.join(self._name(i) for i in a['endorse']) or 'nobody'}"
                        + (
                            f"; objected by {', '.join(self._name(i) for i in a['object'])}"
                            if a["object"]
                            else ""
                        )
                        + ")"
                        for a in self.agreement
                    ][:40],
                }
            )
        dissent = self.dissent()
        blocks.append(
            {"type": "bullets", "items": [f"Dissent: {_cell(d, 500)}" for d in dissent][:40]}
            if dissent
            else {"type": "paragraph", "text": "No dissent was recorded."}
        )
        if self.signatures:
            signed = [self._name(s["member_id"]) for s in self.signatures if s["action"] == "sign"]
            blocks.append({"type": "paragraph", "text": f"Signed by: {', '.join(signed) or 'nobody'}."})
        section = Section.model_validate({"id": "council-process", "heading": HOW_HEADING, "blocks": blocks})
        report.appendix = [s for s in report.appendix if s.id != "council-process"] + [section]

    def summary(self) -> str:
        """The debate's outcome for the report pipeline's evidence (empty before converging)."""
        if not self.agreement:
            return ""
        return "## The council's debate\n" + self.record_text()
