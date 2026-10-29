"""Compose a council for a task and pick a model for every seat.

Two independent automations, used by run creation (``RunIn.auto_council`` / ``auto_models``) and by the
composer's preview (``POST /runs/plan``):

* **Roles.** An LLM planner reads the topic and description and picks seats from the role catalogue
  (how many, which roles, and a one-line focus for each). If no model is reachable or its answer is
  unusable, a keyword heuristic composes the council instead, so planning never blocks a run.
* **Models.** Every role has a capability tier (``ROLE_TIER``): deep-reasoning seats (chair, analyst,
  critic, architect, coder) get the strongest model available, everyday seats a balanced one, and
  high-volume overseers that read every thought (monitor) a fast, cheap one. Models are tiered by their
  known price, else by their name, among the routes the owner can actually use.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.agents.roles import DEFAULT_ROSTERS, ROLES, roster_for
from argus.core.logging import get_logger
from argus.db.models import ProviderConfig
from argus.providers.catalog import KINDS
from argus.providers.cli.provider import CLI_PROVIDERS, SUGGESTED_MODELS
from argus.providers.pricing import get_price
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import LLMRouter, Route

log = get_logger(__name__)

Tier = Literal["strong", "balanced", "fast"]
TIER_ORDER: tuple[Tier, ...] = ("strong", "balanced", "fast")

ROLE_TIER: dict[str, Tier] = {
    "chair": "strong",
    "analyst": "strong",
    "critic": "strong",
    "architect": "strong",
    "coder": "strong",
    "researcher": "balanced",
    "writer": "balanced",
    "tester": "balanced",
    "designer": "balanced",
    "assistant": "balanced",
    "security": "balanced",
    "monitor": "fast",
}

MIN_SEATS, MAX_SEATS = 1, 12
DEFAULT_SIZES = {"chat": 2, "coding": 6, "research": 6, "general": 5}
PLANNER_MAX_TOKENS = 1500

# Matched against whole name tokens ("gemini" is not "mini"): words, and parameter sizes like "8b".
_STRONG_WORDS = {"opus", "fable", "ultra", "pro", "large", "reasoner", "o1", "o3", "max", "r1"}
_STRONG_PREFIXES = ("gpt-5", "grok-4", "deepseek-r")
_FAST_WORDS = {"haiku", "mini", "nano", "flash", "lite", "small", "instant", "tiny"}


def _billions(token: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)b", token)
    return float(m.group(1)) if m else None


def model_tier(model: str) -> Tier:
    """Capability tier of a model: by output price when known (USD / Mtok), else by its name."""
    price = get_price(model)
    if price is not None:
        return "strong" if price.output >= 15 else "fast" if price.output < 6 else "balanced"
    name = model.lower().rsplit("/", 1)[-1]
    tokens = [t for t in re.split(r"[-_.:/ ]+", name) if t]
    sizes = [b for b in map(_billions, tokens) if b is not None]
    if any(t in _FAST_WORDS for t in tokens) or (sizes and max(sizes) < 10):
        return "fast"
    if (
        any(t in _STRONG_WORDS for t in tokens)
        or name.startswith(_STRONG_PREFIXES)
        or (sizes and max(sizes) >= 300)
    ):
        return "strong"
    return "balanced"


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    tier: Tier
    auth: Literal["api_key", "account", "system"]
    preferred: bool = False


@dataclass
class PlannedSeat:
    role: str
    name: str
    focus: str = ""
    reason: str = ""
    provider: str = ""
    model: str = ""
    tier: Tier = "balanced"
    overseer: bool = False


@dataclass
class CouncilPlan:
    seats: list[PlannedSeat]
    rationale: str
    planner: Literal["llm", "heuristic", "roster"]
    planner_route: dict[str, str] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seats": [asdict(s) for s in self.seats],
            "rationale": self.rationale,
            "planner": self.planner,
            "planner_route": self.planner_route,
            "notes": self.notes,
        }


# ------------------------------------------------------------------ models
async def candidates(
    db: AsyncSession, llm: LLMRouter, owner_id: uuid.UUID, preferred: Route | None = None
) -> list[Candidate]:
    """Every provider/model pair the owner can run, best-known models first. Mock only if enabled."""
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str, auth: Literal["api_key", "account", "system"]) -> None:
        if not model or (provider, model) in seen:
            return
        seen.add((provider, model))
        pref = preferred is not None and preferred.provider == provider
        out.append(Candidate(provider, model, model_tier(model), auth, pref))

    configs = (
        await db.execute(
            select(ProviderConfig)
            .where(ProviderConfig.owner_id == owner_id, ProviderConfig.enabled.is_(True))
            .order_by(ProviderConfig.name)
        )
    ).scalars()
    for cfg in configs:
        spec = KINDS.get(cfg.kind)
        for model in [cfg.default_model or "", *(spec.suggested_models if spec else ())]:
            add(cfg.name, model, "api_key")
    for name in llm.available_system_providers():
        spec = KINDS.get(name)
        for model in spec.suggested_models if spec else ():
            add(name, model, "system")
    cli = getattr(llm, "cli", None)
    for name in cli.available_names() if cli else []:
        for model in SUGGESTED_MODELS.get(CLI_PROVIDERS[name], ()):
            add(name, model, "account")
    if preferred is not None:
        add(preferred.provider, preferred.model, "api_key")
    return out


def pick_model(
    role: str, pool: list[Candidate], used: Counter[tuple[str, str]] | None = None
) -> Candidate | None:
    """Best candidate for ``role``: its tier, else the nearest tier (stronger before weaker).

    Within a tier: the provider the user picked, then API keys before CLI accounts, then the least used
    model so far (spreading load across providers), then a stable order.
    """
    if not pool:
        return None
    want = ROLE_TIER.get(role, "balanced")
    i = TIER_ORDER.index(want)
    # Nearest tier first; on a tie prefer the stronger one.
    order = sorted(TIER_ORDER, key=lambda t: (abs(TIER_ORDER.index(t) - i), TIER_ORDER.index(t)))
    used = used if used is not None else Counter()
    for tier in order:
        options = [c for c in pool if c.tier == tier]
        if options:
            return min(
                options,
                key=lambda c: (
                    not c.preferred,
                    c.auth == "account",
                    used[(c.provider, c.model)],
                    pool.index(c),
                ),
            )
    return None


def assign_models(seats: list[PlannedSeat], pool: list[Candidate], fallback: Route | None) -> list[str]:
    """Fill ``provider``/``model``/``tier`` on every seat in place; returns notes for the user."""
    notes: list[str] = []
    used: Counter[tuple[str, str]] = Counter()
    for seat in seats:
        choice = pick_model(seat.role, pool, used)
        if choice is None:
            if fallback is None:
                notes.append("No model is available: connect a provider first.")
                break
            seat.provider, seat.model, seat.tier = (
                fallback.provider,
                fallback.model,
                model_tier(fallback.model),
            )
            continue
        used[(choice.provider, choice.model)] += 1
        seat.provider, seat.model, seat.tier = choice.provider, choice.model, choice.tier
        want = ROLE_TIER.get(seat.role, "balanced")
        if choice.tier != want:
            notes.append(f"No {want} model connected for {seat.name}; using {choice.model} ({choice.tier}).")
    return list(dict.fromkeys(notes))


# ------------------------------------------------------------------ roles
def _named(roles: list[tuple[str, str, str]]) -> list[PlannedSeat]:
    """Seats from (role, focus, reason), titled like the default rosters ("Researcher 2")."""
    taken: Counter[str] = Counter()
    seats = []
    for role, focus, reason in roles:
        spec = ROLES[role]
        taken[role] += 1
        name = spec.title if taken[role] == 1 else f"{spec.title} {taken[role]}"
        seats.append(PlannedSeat(role=role, name=name, focus=focus, reason=reason, overseer=spec.overseer))
    return seats


_KEYWORDS: list[tuple[re.Pattern[str], list[str], str]] = [
    (
        re.compile(r"\b(code|implement|bug|refactor|api|function|script|library|cli|test|deploy|build)\b"),
        ["architect", "coder", "tester"],
        "the task involves writing or changing software",
    ),
    (
        re.compile(
            r"\b(research|paper|papers|literature|survey|sources?|compare|market|evidence|study|studies|news)\b"
        ),
        ["researcher", "researcher", "analyst"],
        "the task needs sources gathered and weighed",
    ),
    (
        re.compile(r"\b(write|report|article|blog|essay|document|summar\w+|draft|plan|proposal)\b"),
        ["writer"],
        "the task ends in a written deliverable",
    ),
    (
        re.compile(r"\b(design|ui|ux|interface|layout|slides?|deck|presentation)\b"),
        ["designer"],
        "the deliverable's presentation matters",
    ),
    (
        re.compile(r"\b(security|secure|vulnerab\w*|threat|attack|privacy|secrets?|auth\w*)\b"),
        ["security"],
        "the task has security implications",
    ),
    (
        re.compile(r"\b(analy[sz]\w+|data|metrics|numbers|statistics|forecast|evaluate|trade-?offs?)\b"),
        ["analyst"],
        "the task needs careful reasoning over evidence",
    ),
]


def heuristic_plan(topic: str, description: str, mode: str, max_size: int) -> list[PlannedSeat]:
    """Keyword-driven council: the chair, the specialists the text asks for, then a critic and a monitor."""
    text = f"{topic}\n{description}".lower()
    if mode == "chat":
        roles = [
            ("assistant", "Answer the user directly.", "chat mode"),
            ("critic", "Check the answer.", "keeps answers honest"),
        ]
        return _named(roles[: max(1, max_size)])
    picked: list[tuple[str, str, str]] = [
        ("chair", "Break the task down, assign work and merge the result.", "every council needs a lead")
    ]
    for pattern, roles, why in _KEYWORDS:
        if pattern.search(text):
            for r in roles:
                picked.append((r, "", why))
    if len(picked) == 1:  # nothing matched: fall back to the mode's default specialists
        for r in DEFAULT_ROSTERS.get(mode, DEFAULT_ROSTERS["general"])[1:4]:
            if not ROLES[r].overseer:
                picked.append((r, "", f"default specialist for {mode} mode"))
    if mode == "coding" and not any(r == "coder" for r, _, _ in picked):
        picked += [
            ("architect", "", "coding mode"),
            ("coder", "", "coding mode"),
            ("tester", "", "coding mode"),
        ]
    picked.append(("critic", "Challenge the reasoning and the result.", "independent review"))
    picked.append(("monitor", "Watch for drift, loops and guardrail breaches.", "oversight"))
    # Longer briefs get a second hand on the busiest role.
    if len(description) > 1500 and len(picked) < max_size:
        busiest = Counter(r for r, _, _ in picked if not ROLES[r].overseer and r != "chair").most_common(1)
        if busiest:
            picked.insert(2, (busiest[0][0], "", "the brief is long"))
    return _named(_trim(picked, max_size))


def _trim(picked: list[tuple[str, str, str]], max_size: int) -> list[tuple[str, str, str]]:
    """Cap the council at ``max_size`` seats, dropping duplicates of a role before unique roles."""
    out = list(picked)
    while len(out) > max_size:
        counts = Counter(r for r, _, _ in out)
        dup = next(
            (i for i in range(len(out) - 1, 0, -1) if counts[out[i][0]] > 1 and out[i][0] != "monitor"), None
        )
        drop = (
            dup
            if dup is not None
            else next(
                (i for i in range(len(out) - 1, 0, -1) if out[i][0] not in ("monitor", "critic")),
                len(out) - 1,
            )
        )
        out.pop(drop)
    return out


def _planner_prompt(topic: str, description: str, mode: str, max_size: int) -> list[Message]:
    catalogue = "\n".join(
        f"- {r.key}: {r.title}{' (overseer: watches the others)' if r.overseer else ''}. {r.description}"
        for r in ROLES.values()
    )
    system = (
        "You design the team of AI agents (a council) for one task. Choose only roles from the catalogue. "
        "Pick as few seats as the task really needs, between 1 and the maximum; repeat a role when the "
        "work can be split (e.g. two researchers on different angles). Except in chat mode, include exactly "
        "one chair and at least one overseer (monitor or security). Give every seat a one-line focus "
        "specific to this task. Reply with JSON only, no prose: "
        '{"rationale": "...", "seats": [{"role": "<key>", "focus": "...", "reason": "..."}]}'
    )
    user = (
        f"Mode: {mode}\nMaximum seats: {max_size}\nTask: {topic}\n"
        + (f"Details:\n{description[:6000]}\n" if description.strip() else "")
        + f"\nRole catalogue:\n{catalogue}"
    )
    return [Message.system(system), Message.user(user)]


def parse_planner_reply(text: str, mode: str, max_size: int) -> tuple[list[PlannedSeat], str] | None:
    """Validate the planner's JSON; None when it is unusable (the heuristic plans instead)."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    raw = data.get("seats") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    picked: list[tuple[str, str, str]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        role = str(s.get("role", "")).strip().lower()
        if role not in ROLES:
            continue
        picked.append((role, str(s.get("focus", ""))[:300].strip(), str(s.get("reason", ""))[:300].strip()))
    if not picked:
        return None
    if mode != "chat":
        if not any(r == "chair" for r, _, _ in picked):
            picked.insert(
                0,
                (
                    "chair",
                    "Break the task down, assign work and merge the result.",
                    "added: every council needs a lead",
                ),
            )
        chairs = [i for i, (r, _, _) in enumerate(picked) if r == "chair"]
        for i in reversed(chairs[1:]):
            picked.pop(i)
        if not any(ROLES[r].overseer for r, _, _ in picked) and max_size >= 3:
            picked.append(("monitor", "Watch for drift, loops and guardrail breaches.", "added: oversight"))
    rationale = str(data.get("rationale", ""))[:600].strip()
    return _named(_trim(picked, max_size)), rationale


async def plan_council(
    db: AsyncSession,
    llm: LLMRouter,
    owner_id: uuid.UUID,
    *,
    topic: str,
    description: str,
    mode: str,
    max_size: int = MAX_SEATS,
    preferred: Route | None = None,
    roles: list[str] | None = None,
    auto_roles: bool = True,
    auto_models: bool = True,
) -> CouncilPlan:
    """Plan the seats (and/or their models) for a task. Never raises for a planner failure."""
    max_size = max(MIN_SEATS, min(max_size, MAX_SEATS))
    pool = await candidates(db, llm, owner_id, preferred) if (auto_models or auto_roles) else []
    notes: list[str] = []
    planner: Literal["llm", "heuristic", "roster"] = "roster"
    planner_route: dict[str, str] | None = None
    rationale = ""
    if roles is not None or not auto_roles:
        wanted = roles if roles is not None else roster_for(mode, min(max_size, DEFAULT_SIZES.get(mode, 5)))
        seats = _named([(r, "", "") for r in wanted if r in ROLES])
    else:
        seats = []
        # Plan with the user's chosen model, else the strongest one available.
        route = preferred
        if route is None:
            best = pick_model("chair", pool)
            route = Route(best.provider, best.model) if best else None
        if route is not None:
            try:
                resp = await llm.complete(
                    CompletionRequest(
                        model=route.model,
                        messages=_planner_prompt(topic, description, mode, max_size),
                        max_tokens=PLANNER_MAX_TOKENS,
                        temperature=0,
                        metadata={"purpose": "plan_council"},
                    ),
                    route,
                    db=db,
                    owner_id=owner_id,
                )
                parsed = parse_planner_reply(resp.content, mode, max_size)
                if parsed:
                    seats, rationale = parsed
                    planner, planner_route = "llm", {"provider": route.provider, "model": route.model}
                else:
                    notes.append(
                        "The planner's answer was unusable, so the council was composed from keywords."
                    )
            except Exception as exc:  # planning must never block a run
                log.warning("autocouncil.planner_failed", error=type(exc).__name__)
                notes.append(
                    "The planner model could not be reached, so the council was composed from keywords."
                )
        if not seats:
            seats = heuristic_plan(topic, description, mode, max_size)
            planner = "heuristic"
            rationale = rationale or "Composed from the roles the task description calls for."
    if auto_models:
        notes += assign_models(seats, pool, preferred)
    elif preferred is not None:
        for s in seats:
            s.provider, s.model, s.tier = preferred.provider, preferred.model, model_tier(preferred.model)
    return CouncilPlan(
        seats=seats, rationale=rationale, planner=planner, planner_route=planner_route, notes=notes
    )


def snapshot_members(seats: list[PlannedSeat]) -> list[dict[str, Any]]:
    """Run snapshot members for planned seats (same shape as ``ephemeral_roster``)."""
    members = []
    for i, s in enumerate(seats):
        spec = ROLES[s.role]
        prompt = spec.system_prompt + (f"\n\nYour focus in this council: {s.focus}" if s.focus else "")
        members.append(
            {
                "id": str(uuid.uuid4()),
                "name": s.name,
                "role": s.role,
                "provider": s.provider,
                "model": s.model,
                "system_prompt": prompt,
                "temperature": None,
                "position": i,
                "enabled": True,
                "config": {"tools": list(spec.default_tools), "overseer": spec.overseer, "focus": s.focus},
            }
        )
    return members
