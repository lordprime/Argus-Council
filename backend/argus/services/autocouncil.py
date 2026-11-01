"""Compose a council for a task and pick a model for every seat.

Two independent automations, used by run creation (``RunIn.auto_council`` / ``auto_models``) and by the
composer's preview (``POST /runs/plan``):

* **Roles.** An LLM planner reads the topic and description and picks seats from the role catalogue
  (how many, which roles, and a one-line focus for each). If no model is reachable or its answer is
  unusable, a keyword heuristic composes the council instead, so planning never blocks a run.
* **Models.** Every role states what it needs (``ROLE_NEEDS``): which competences matter (reasoning,
  coding, writing, speed), the floor below which a model is unfit, and how many tokens a seat typically
  reads and writes over a run. Every route the owner can use is scored for the seat on competence
  (``argus.providers.profiles``) against expected spend (input and output prices per token), weighted
  by a strategy (quality, balanced, economy) and scaled to the run's budget. The council is then fitted
  to the budget: the cheapest downgrades first, then, for an automatic council, optional seats.
* **Steps.** When the chair's plan does not name a member for a step, ``choose_assignee`` gives it to
  the member whose model is most competent for the kind of work, net of load and cost.
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
from argus.providers.profiles import (
    CallPrice,
    Dimension,
    ModelProfile,
    name_tier,
    price_for,
    profile_for,
)
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import LLMRouter, Route

log = get_logger(__name__)

Tier = Literal["strong", "balanced", "fast"]
TIER_ORDER: tuple[Tier, ...] = ("strong", "balanced", "fast")
Strategy = Literal["quality", "balanced", "economy"]

# The capability class each role is designed around (shown in the plan; the choice itself is made by
# competence and cost, see ROLE_NEEDS).
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


@dataclass(frozen=True)
class RoleNeed:
    """What a seat needs from its model, and how much it typically uses it in one run.

    ``weights`` say which competences matter; below ``floor`` a model is not fit for the seat, above
    ``target`` extra competence earns little. ``tokens_in``/``tokens_out`` are one typical call and
    ``calls`` the calls a seat makes over a run, so a seat's expected spend is
    ``calls x (tokens_in x input price + tokens_out x output price)``. Overseers read every thought:
    many small calls, which is why speed and input price dominate for them.
    """

    weights: dict[Dimension, float]
    floor: float
    target: float
    tokens_in: int
    tokens_out: int
    calls: int


ROLE_NEEDS: dict[str, RoleNeed] = {
    "chair": RoleNeed({"reasoning": 0.6, "writing": 0.3, "coding": 0.1}, 80, 90, 12_000, 1_500, 6),
    "analyst": RoleNeed({"reasoning": 0.7, "writing": 0.2, "coding": 0.1}, 78, 88, 16_000, 2_000, 4),
    "critic": RoleNeed({"reasoning": 0.7, "writing": 0.3}, 78, 88, 14_000, 1_200, 4),
    "architect": RoleNeed({"reasoning": 0.5, "coding": 0.5}, 80, 90, 12_000, 2_500, 3),
    "coder": RoleNeed({"coding": 0.8, "reasoning": 0.2}, 80, 90, 20_000, 4_000, 8),
    "researcher": RoleNeed({"reasoning": 0.4, "writing": 0.3, "speed": 0.3}, 65, 75, 24_000, 1_500, 8),
    "writer": RoleNeed({"writing": 0.8, "reasoning": 0.2}, 72, 85, 30_000, 6_000, 3),
    "tester": RoleNeed({"coding": 0.6, "reasoning": 0.3, "speed": 0.1}, 68, 80, 16_000, 2_000, 5),
    "designer": RoleNeed({"writing": 0.5, "reasoning": 0.3, "coding": 0.2}, 65, 78, 10_000, 2_000, 3),
    "assistant": RoleNeed({"reasoning": 0.4, "writing": 0.4, "speed": 0.2}, 65, 80, 6_000, 1_200, 3),
    "security": RoleNeed({"reasoning": 0.6, "coding": 0.3, "speed": 0.1}, 70, 82, 8_000, 400, 20),
    "monitor": RoleNeed({"speed": 0.6, "reasoning": 0.4}, 50, 60, 4_000, 200, 40),
}
DEFAULT_NEED = RoleNeed({"reasoning": 0.5, "writing": 0.5}, 65, 80, 10_000, 1_500, 4)

# How strongly expected spend counts against competence: penalty points for spending the whole budget.
STRATEGY_WEIGHT: dict[Strategy, float] = {"quality": 10.0, "balanced": 50.0, "economy": 200.0}
REFERENCE_BUDGET_USD = 5.0  # what the penalty is scaled against when the run has no dollar budget
BUDGET_SHARE = 0.8  # seats plan to spend at most this share of the budget; the rest is headroom
PREFERENCE_BONUS = 5.0  # the provider the user picked wins near-ties
QUOTA_PENALTY = 3.0  # subscription CLI accounts cost nothing per token but have usage limits
REUSE_PENALTY = 0.75  # per seat already on the same model: spreads load and rate limits

MIN_SEATS, MAX_SEATS = 1, 12
DEFAULT_SIZES = {"chat": 2, "coding": 6, "research": 6, "general": 5}
PLANNER_MAX_TOKENS = 1500


def model_tier(model: str) -> Tier:
    """Capability tier of a model: by output price when known (USD / Mtok), else by its name."""
    price = get_price(model)
    if price is not None:
        return "strong" if price.output >= 15 else "fast" if price.output < 6 else "balanced"
    return name_tier(model)


def role_need(role: str) -> RoleNeed:
    return ROLE_NEEDS.get(role, DEFAULT_NEED)


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    tier: Tier
    auth: Literal["api_key", "account", "system"]
    preferred: bool = False
    self_hosted: bool = False

    @property
    def profile(self) -> ModelProfile:
        return profile_for(self.model)

    @property
    def price(self) -> CallPrice:
        return price_for(self.model, auth=self.auth, self_hosted=self.self_hosted)


@dataclass(frozen=True)
class Fit:
    """How well a candidate suits a role: competence, expected spend, and the resulting utility."""

    candidate: Candidate
    competence: float
    seat_usd: float
    utility: float
    meets_floor: bool


def fit(
    role: str,
    c: Candidate,
    *,
    strategy: Strategy = "balanced",
    budget_usd: float | None = None,
    reuse: int = 0,
) -> Fit:
    """Score ``c`` for a seat of ``role``.

    utility = capped competence (full credit up to the role's target, a quarter above it, a double
    penalty below its floor) - spend penalty (the seat's expected spend as a share of the budget,
    weighted by the strategy) + the user's preference - subscription quota - reuse.
    """
    need = role_need(role)
    competence = c.profile.score(need.weights)
    value = min(competence, need.target) + 0.25 * max(0.0, competence - need.target)
    if competence < need.floor:
        value -= 2 * (need.floor - competence)
    seat_usd = need.calls * c.price.call_usd(need.tokens_in, need.tokens_out)
    scale = REFERENCE_BUDGET_USD if budget_usd is None else max(budget_usd, 0.01)
    utility = (
        value
        - STRATEGY_WEIGHT[strategy] * seat_usd / scale
        + (PREFERENCE_BONUS if c.preferred else 0.0)
        - (QUOTA_PENALTY if c.price.basis == "subscription" else 0.0)
        - REUSE_PENALTY * reuse
    )
    return Fit(c, round(competence, 1), round(seat_usd, 4), round(utility, 2), competence >= need.floor)


def ranked(
    role: str,
    pool: list[Candidate],
    used: Counter[tuple[str, str]] | None = None,
    *,
    strategy: Strategy = "balanced",
    budget_usd: float | None = None,
) -> list[Fit]:
    """Every candidate scored for ``role``, best first (ties keep the pool's order)."""
    used = used if used is not None else Counter()
    fits = [
        fit(role, c, strategy=strategy, budget_usd=budget_usd, reuse=used[(c.provider, c.model)])
        for c in pool
    ]
    order = {id(f): i for i, f in enumerate(fits)}
    return sorted(fits, key=lambda f: (-f.utility, order[id(f)]))


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
    competence: float = 0.0  # 0-100 for this role
    est_usd: float = 0.0  # expected spend of the seat over a run
    cost_basis: str = ""  # listed | reference | estimated | subscription | self_hosted
    model_reason: str = ""


@dataclass
class CouncilPlan:
    seats: list[PlannedSeat]
    rationale: str
    planner: Literal["llm", "heuristic", "roster"]
    planner_route: dict[str, str] | None = None
    notes: list[str] = field(default_factory=list)
    strategy: Strategy = "balanced"
    budget_usd: float | None = None
    estimated_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "seats": [asdict(s) for s in self.seats],
            "rationale": self.rationale,
            "planner": self.planner,
            "planner_route": self.planner_route,
            "notes": self.notes,
            "strategy": self.strategy,
            "budget_usd": self.budget_usd,
            "estimated_usd": self.estimated_usd,
        }


# ------------------------------------------------------------------ models
async def candidates(
    db: AsyncSession, llm: LLMRouter, owner_id: uuid.UUID, preferred: Route | None = None
) -> list[Candidate]:
    """Every provider/model pair the owner can run, best-known models first. Mock only if enabled."""
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()

    def add(
        provider: str, model: str, auth: Literal["api_key", "account", "system"], self_hosted: bool = False
    ) -> None:
        if not model or (provider, model) in seen:
            return
        seen.add((provider, model))
        pref = preferred is not None and preferred.provider == provider
        out.append(Candidate(provider, model, model_tier(model), auth, pref, self_hosted))

    configs = (
        await db.execute(
            select(ProviderConfig)
            .where(ProviderConfig.owner_id == owner_id, ProviderConfig.enabled.is_(True))
            .order_by(ProviderConfig.name)
        )
    ).scalars()
    for cfg in configs:
        spec = KINDS.get(cfg.kind)
        hosted = bool(spec and spec.self_hosted)
        for model in [cfg.default_model or "", *(spec.suggested_models if spec else ())]:
            add(cfg.name, model, "api_key", hosted)
    for name in llm.available_system_providers():
        spec = KINDS.get(name)
        for model in spec.suggested_models if spec else ():
            add(name, model, "system", bool(spec and spec.self_hosted))
    cli = getattr(llm, "cli", None)
    for name in cli.available_names() if cli else []:
        for model in SUGGESTED_MODELS.get(CLI_PROVIDERS[name], ()):
            add(name, model, "account")
    if preferred is not None:
        spec = KINDS.get(preferred.provider)
        add(preferred.provider, preferred.model, "api_key", bool(spec and spec.self_hosted))
    return out


def pick_model(
    role: str,
    pool: list[Candidate],
    used: Counter[tuple[str, str]] | None = None,
    *,
    strategy: Strategy = "balanced",
    budget_usd: float | None = None,
) -> Candidate | None:
    """The candidate with the best competence-for-cost for ``role`` (see :func:`fit`)."""
    best = ranked(role, pool, used, strategy=strategy, budget_usd=budget_usd)
    return best[0].candidate if best else None


def _apply(seat: PlannedSeat, f: Fit, why: str = "") -> None:
    c = f.candidate
    seat.provider, seat.model, seat.tier = c.provider, c.model, model_tier(c.model)
    seat.competence, seat.est_usd, seat.cost_basis = f.competence, f.seat_usd, c.price.basis
    seat.model_reason = why or (
        f"competence {f.competence:.0f} for {seat.role}, "
        f"about ${f.seat_usd:.2f} per run ({c.price.basis} price)"
    )


def assign_models(
    seats: list[PlannedSeat],
    pool: list[Candidate],
    fallback: Route | None,
    *,
    strategy: Strategy = "balanced",
    budget_usd: float | None = None,
) -> list[str]:
    """Fill the model fields of every seat in place; returns notes for the user.

    Each seat gets the candidate with the best utility for its role. When the seats' expected spend
    exceeds ``BUDGET_SHARE`` of the budget, the cheapest downgrades (least utility lost per dollar
    saved, staying above each role's floor) are applied until it fits.
    """
    notes: list[str] = []
    used: Counter[tuple[str, str]] = Counter()
    fits: dict[int, list[Fit]] = {}
    for i, seat in enumerate(seats):
        options = ranked(seat.role, pool, used, strategy=strategy, budget_usd=budget_usd)
        if not options:
            if fallback is None:
                notes.append("No model is available: connect a provider first.")
                break
            seat.provider, seat.model = fallback.provider, fallback.model
            seat.tier = model_tier(fallback.model)
            seat.cost_basis = price_for(fallback.model).basis
            seat.model_reason = "the only route available"
            continue
        fits[i] = options
        choice = options[0]
        used[(choice.candidate.provider, choice.candidate.model)] += 1
        _apply(seat, choice)
        if not choice.meets_floor:
            notes.append(
                f"No connected model is fully competent for {seat.name} "
                f"(needs {role_need(seat.role).floor:.0f}); "
                f"using {choice.candidate.model} ({choice.competence:.0f})."
            )
    if budget_usd is not None:
        notes += _fit_budget(seats, fits, budget_usd)
    estimated = sorted({s.model for s in seats if s.cost_basis == "estimated"})
    if estimated:
        notes.append(
            f"Prices of {', '.join(estimated)} are estimated from the model name; "
            "set ARGUS_MODEL_PRICING or ARGUS_MODEL_PROFILES for exact costs."
        )
    return list(dict.fromkeys(notes))


def _fit_budget(seats: list[PlannedSeat], fits: dict[int, list[Fit]], budget_usd: float) -> list[str]:
    cap = budget_usd * BUDGET_SHARE
    notes: list[str] = []
    while sum(s.est_usd for s in seats) > cap:
        best: tuple[float, int, Fit] | None = None
        for i, options in fits.items():
            seat = seats[i]
            route = (seat.provider, seat.model)
            current = next((f for f in options if (f.candidate.provider, f.candidate.model) == route), None)
            if current is None:
                continue
            for alt in options:
                saved = current.seat_usd - alt.seat_usd
                if saved <= 1e-9 or (current.meets_floor and not alt.meets_floor):
                    continue
                cost = max(current.utility - alt.utility, 0.0) / saved
                if best is None or cost < best[0]:
                    best = (cost, i, alt)
        if best is None:
            break
        _, i, alt = best
        old = seats[i].model
        _apply(seats[i], alt, f"downgraded from {old} to fit the ${budget_usd:g} budget")
        notes.append(f"{seats[i].name}: {old} -> {alt.candidate.model} to fit the ${budget_usd:g} budget.")
    total = sum(s.est_usd for s in seats)
    if total > cap:
        notes.append(
            f"The council is expected to spend about ${total:.2f}, above {BUDGET_SHARE:.0%} of the "
            f"${budget_usd:g} budget: raise the budget, use fewer seats or the economy strategy."
        )
    return notes


def shed_for_budget(seats: list[PlannedSeat], budget_usd: float, keep: int = 2) -> list[str]:
    """Drop optional seats (repeated roles first, never the chair or the last overseer) until the
    council's expected spend fits the budget. Used when the roles were composed automatically."""
    cap = budget_usd * BUDGET_SHARE
    notes: list[str] = []
    while sum(s.est_usd for s in seats) > cap and len(seats) > keep:
        counts = Counter(s.role for s in seats)
        lone_overseer = sum(1 for s in seats if s.overseer) <= 1
        removable = [s for s in seats if s.role != "chair" and not (s.overseer and lone_overseer)]
        dupes = [s for s in removable if counts[s.role] > 1]
        pool = dupes or removable
        if not pool:
            break
        drop = max(pool, key=lambda s: (s.est_usd, seats.index(s)))
        seats.remove(drop)
        notes.append(f"Left out {drop.name} (about ${drop.est_usd:.2f}) to stay within the budget.")
    return notes


def estimated_spend(seats: list[PlannedSeat]) -> float:
    return round(sum(s.est_usd for s in seats), 4)


# ------------------------------------------------------------------ step assignment
_TASK_SKILLS: list[tuple[re.Pattern[str], dict[Dimension, float]]] = [
    (
        re.compile(r"\b(code|implement|refactor|debug|fix|test|function|module|script|api|build)\w*", re.I),
        {"coding": 0.7, "reasoning": 0.3},
    ),
    (
        re.compile(r"\b(write|draft|report|summar\w*|document|edit|polish|narrative|article)\w*", re.I),
        {"writing": 0.7, "reasoning": 0.3},
    ),
    (
        re.compile(r"\b(gather|collect|search|find|sources?|survey|scan|look up|compile)\w*", re.I),
        {"reasoning": 0.4, "writing": 0.2, "speed": 0.4},
    ),
]


def task_weights(text: str) -> dict[Dimension, float]:
    """Which competences a piece of work leans on, from its wording (reasoning when unclear)."""
    weights: dict[Dimension, float] = {"reasoning": 0.5, "writing": 0.25, "coding": 0.25}
    for pattern, w in _TASK_SKILLS:
        if pattern.search(text):
            return w
    return weights


def choose_assignee(
    text: str, members: list[Any], load: Counter[str], *, budget_usd: float | None = None
) -> Any:
    """Best council member for a step nobody was named for.

    Competence on what the step needs (by its wording), plus affinity when the step's words match the
    member's role, minus a load penalty (each step already given to the member) and its per-call spend.
    """
    weights = task_weights(text)
    lowered = text.lower()
    scale = REFERENCE_BUDGET_USD if budget_usd is None else max(budget_usd, 0.01)

    def score(m: Any) -> float:
        affinity = 12.0 if any(p.search(lowered) and m.role in roles for p, roles, _ in _KEYWORDS) else 0.0
        if m.role in lowered:
            affinity += 4.0
        need = role_need(m.role)
        call = price_for(m.model, auth="account" if m.provider.endswith("-cli") else "api_key").call_usd(
            need.tokens_in, need.tokens_out
        )
        return (
            profile_for(m.model).score(weights)
            + affinity
            - 6.0 * load[m.id]
            - STRATEGY_WEIGHT["balanced"] * call / scale
            - (6.0 if m.role == "chair" else 0.0)  # the chair coordinates; give it work last
        )

    return max(members, key=lambda m: (score(m), -members.index(m)))


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


def _planner_prompt(
    topic: str, description: str, mode: str, max_size: int, cost_hint: str = ""
) -> list[Message]:
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
        + (f"Cost: {cost_hint}\n" if cost_hint else "")
        + f"\nRole catalogue:\n{catalogue}"
    )
    return [Message.system(system), Message.user(user)]


def cost_hint(pool: list[Candidate], budget_usd: float | None, strategy: Strategy) -> str:
    """One line telling the planner what seats cost with the connected models, so it sizes the council."""
    if not pool:
        return ""
    per_role = [
        ranked(r, pool, strategy=strategy, budget_usd=budget_usd)[0].seat_usd
        for r in ("chair", "researcher", "analyst", "writer", "monitor")
    ]
    lo, hi = min(per_role), max(per_role)
    if hi <= 0:
        return "the connected models cost nothing per token (subscription or self-hosted)."
    budget = f"The run budget is ${budget_usd:g}; " if budget_usd is not None else ""
    return (
        f"{budget}a seat costs about ${lo:.2f}-${hi:.2f} over a run with the connected models. "
        "Every seat must earn its cost: prefer fewer, well-focused seats."
    )


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
    strategy: Strategy = "balanced",
    budget_usd: float | None = None,
) -> CouncilPlan:
    """Plan the seats (and/or their models) for a task. Never raises for a planner failure.

    ``strategy`` trades competence against cost (quality, balanced, economy) and ``budget_usd`` is the
    run's dollar budget: the planner is told what seats cost, models are downgraded to fit it, and an
    automatically composed council sheds optional seats if it still does not fit.
    """
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
            best = pick_model("chair", pool, strategy=strategy, budget_usd=budget_usd)
            route = Route(best.provider, best.model) if best else None
        if route is not None:
            try:
                resp = await llm.complete(
                    CompletionRequest(
                        model=route.model,
                        messages=_planner_prompt(
                            topic, description, mode, max_size, cost_hint(pool, budget_usd, strategy)
                        ),
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
        notes += assign_models(seats, pool, preferred, strategy=strategy, budget_usd=budget_usd)
        if budget_usd is not None and auto_roles and roles is None:
            shed = shed_for_budget(seats, budget_usd)
            if shed:
                # The remaining notes about the old, larger council no longer apply.
                notes = [n for n in notes if "expected to spend" not in n] + shed
    elif preferred is not None:
        for s in seats:
            s.provider, s.model, s.tier = preferred.provider, preferred.model, model_tier(preferred.model)
            s.cost_basis = price_for(preferred.model).basis
            s.competence = round(profile_for(preferred.model).score(role_need(s.role).weights), 1)
    return CouncilPlan(
        seats=seats,
        rationale=rationale,
        planner=planner,
        planner_route=planner_route,
        notes=notes,
        strategy=strategy,
        budget_usd=budget_usd,
        estimated_usd=estimated_spend(seats),
    )


def reassign_roster(
    members: list[dict[str, Any]],
    routes: list[tuple[str, str]],
    *,
    budget_usd: float | None = None,
    preferred: Route | None = None,
) -> list[str]:
    """Pick a model per seat of an existing roster from a fixed set of routes (sub-councils draw on
    the parent council's models). Updates ``members`` in place; returns notes."""
    pool = [
        Candidate(
            p,
            m,
            model_tier(m),
            "account" if p in CLI_PROVIDERS else "api_key",
            preferred is not None and (p, m) == (preferred.provider, preferred.model),
            bool(KINDS.get(p) and KINDS[p].self_hosted),
        )
        for p, m in dict.fromkeys(routes)
    ]
    seats = [PlannedSeat(role=m["role"], name=m["name"]) for m in members]
    notes = assign_models(seats, pool, preferred, budget_usd=budget_usd)
    for m, s in zip(members, seats, strict=True):
        if s.provider and s.model:
            m["provider"], m["model"] = s.provider, s.model
    return notes


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
