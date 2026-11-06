"""Cheaper seats for work that needs a good reasoner but not the seat's own model.

Debate's light moves and context compression read a digest and write a short answer. The newest
small models do that well, at a fraction of a flagship's price. :func:`cheapest_strong` picks, among
the models the owner can run on the seat's own provider, the cheapest one whose reasoning score
(``argus.providers.profiles``) clears a floor; among equal prices the stronger one, which in a family
is the newer. The seat's own model stays the fallback, and the member is otherwise unchanged.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from argus.core.logging import get_logger

if TYPE_CHECKING:
    from argus.runtime.agent import MemberSpec

log = get_logger(__name__)

# What such a call typically sends and gets back (for comparing prices, not for billing).
LIGHT_CALL_TOKENS = (8_000, 800)
CLI_SUFFIX = "-cli"


async def cheapest_strong(ctx: Any, member: MemberSpec, floor: float) -> MemberSpec:
    """``member`` on the cheapest model of its provider that reasons at least ``floor`` well, or
    ``member`` itself (CLI seats, nothing cheaper that qualifies, or no catalogue to choose from)."""
    if member.provider.endswith(CLI_SUFFIX):
        return member
    cache: dict[tuple[str, float], MemberSpec] = ctx.extra.setdefault("cheapest_strong", {})
    key = (member.id + "|" + member.model, floor)
    if key in cache:
        return cache[key]
    try:
        seat = await _choose(ctx, member, floor)
    except Exception as exc:  # choosing a model is an optimisation, never a failure
        log.info("models.cheapest_failed", member=member.name, error=type(exc).__name__)
        seat = member
    cache[key] = seat
    return seat


async def _choose(ctx: Any, member: MemberSpec, floor: float) -> MemberSpec:
    from argus.providers.profiles import price_for, profile_for
    from argus.services.llm import Route
    from argus.tools.spawn import _pool

    tokens_in, tokens_out = LIGHT_CALL_TOKENS

    def cost(model: str, auth: str = "api_key", self_hosted: bool = False) -> float:
        return price_for(model, auth=auth, self_hosted=self_hosted).call_usd(tokens_in, tokens_out)

    own = cost(member.model)
    best: tuple[float, float, str] | None = None
    for c in await _pool(ctx, {member.provider}):
        if c.provider != member.provider or c.model == member.model:
            continue
        reasoning = profile_for(c.model).reasoning
        if reasoning < floor:
            continue
        price = cost(c.model, c.auth, getattr(c, "self_hosted", False))
        if price < own and (best is None or (price, -reasoning) < (best[0], -best[1])):
            best = (price, reasoning, c.model)
    if best is None:
        return member
    return dataclasses.replace(
        member, model=best[2], fallbacks=[Route(member.provider, member.model), *member.fallbacks]
    )
