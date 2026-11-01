"""What a model is good at and what a call to it costs, for choosing models (not for billing).

Selection needs two numbers per model that billing does not: how *competent* it is at the kind of
work a seat does, and what a call is likely to cost before anything has been spent. Both live here.

* **Competence** is a 0-100 score on four dimensions: ``reasoning`` (planning, analysis, judgement),
  ``coding``, ``writing`` and ``speed`` (latency and throughput, which matters for overseers that read
  every thought). Known models ship with a profile; anything else is profiled from its name.
* **Price** comes from :mod:`argus.providers.pricing` (what billing charges) when it knows the model.
  Otherwise from the reference list prices below, then from the model's name tier as a flagged
  estimate. Subscription CLI accounts and self-hosted endpoints cost nothing per token.

The built-in scores are defaults, not benchmarks; operators override or extend both tables with
``ARGUS_MODEL_PROFILES`` (``{"model": {"reasoning": 90, ..., "input": 1.0, "output": 4.0}}``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from argus.providers.pricing import get_price

Dimension = Literal["reasoning", "coding", "writing", "speed"]
DIMENSIONS: tuple[Dimension, ...] = ("reasoning", "coding", "writing", "speed")
CostBasis = Literal["listed", "reference", "estimated", "subscription", "self_hosted"]


@dataclass(frozen=True)
class ModelProfile:
    reasoning: float
    coding: float
    writing: float
    speed: float
    source: Literal["catalog", "configured", "heuristic"] = "catalog"

    def score(self, weights: dict[Dimension, float]) -> float:
        """Competence for a job, as the weighted mean of the dimensions it needs (0-100)."""
        total = sum(weights.values()) or 1.0
        return sum(getattr(self, d) * w for d, w in weights.items()) / total


@dataclass(frozen=True)
class CallPrice:
    """USD per million tokens, and where the figure came from."""

    input: float
    output: float
    basis: CostBasis

    def call_usd(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input + tokens_out * self.output) / 1_000_000


def _p(r: float, c: float, w: float, s: float) -> ModelProfile:
    return ModelProfile(r, c, w, s)


_PROFILES: dict[str, ModelProfile] = {
    "claude-fable-5-1": _p(98, 97, 97, 40),
    "claude-fable-5": _p(97, 96, 96, 40),
    "claude-opus-5-5": _p(95, 95, 93, 52),
    "claude-opus-5": _p(93, 93, 92, 50),
    "claude-opus-4-8": _p(91, 92, 90, 50),
    "claude-opus-4-7": _p(90, 91, 89, 50),
    "claude-opus-4-6": _p(89, 90, 88, 50),
    "claude-sonnet-5-5": _p(87, 89, 87, 72),
    "claude-sonnet-5": _p(85, 87, 85, 72),
    "claude-sonnet-4-6": _p(82, 85, 82, 70),
    "claude-haiku-5-5": _p(72, 72, 73, 96),
    "claude-haiku-4-5": _p(67, 68, 69, 92),
    # Claude Code CLI aliases resolve to the current model of each family.
    "opus": _p(95, 95, 93, 52),
    "sonnet": _p(87, 89, 87, 72),
    "haiku": _p(72, 72, 73, 96),
    "gpt-5": _p(92, 91, 88, 50),
    "gpt-5-mini": _p(78, 78, 75, 85),
    "gpt-5-nano": _p(64, 62, 63, 95),
    "gpt-4.1": _p(78, 81, 80, 75),
    "o3": _p(93, 88, 80, 35),
    "gemini-2.5-pro": _p(89, 87, 85, 60),
    "gemini-2.5-flash": _p(74, 73, 73, 92),
    "mistral-large-latest": _p(77, 75, 77, 70),
    "mistral-small-latest": _p(61, 59, 63, 92),
    "llama-3.3-70b-versatile": _p(67, 63, 67, 95),
    "deepseek-chat": _p(79, 83, 75, 68),
    "deepseek-reasoner": _p(87, 85, 73, 38),
    "grok-4": _p(89, 85, 82, 55),
    "mock-1": _p(50, 50, 50, 100),
    "mock-large": _p(60, 60, 60, 100),
}

# Public list prices (USD / Mtok) of popular models that pricing.py does not bill for. Used only to
# compare candidates before a run; billing never reads them.
_REFERENCE_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.0, 8.0),
    "o3": (2.0, 8.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "mistral-large-latest": (2.0, 6.0),
    "mistral-small-latest": (0.20, 0.60),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "grok-4": (3.0, 15.0),
}

# A name-tier guess when nothing better is known: deliberately on the high side, so an unknown model
# never looks cheaper than a known one of the same class.
_TIER_PRICES: dict[str, tuple[float, float]] = {
    "strong": (5.0, 25.0),
    "balanced": (2.0, 10.0),
    "fast": (0.5, 2.5),
}
_TIER_PROFILES: dict[str, ModelProfile] = {
    "strong": ModelProfile(86, 85, 84, 50, "heuristic"),
    "balanced": ModelProfile(72, 70, 72, 70, "heuristic"),
    "fast": ModelProfile(60, 58, 60, 90, "heuristic"),
}

# Matched against whole name tokens ("gemini" is not "mini"): words, and parameter sizes like "8b".
_STRONG_WORDS = {"opus", "fable", "ultra", "pro", "large", "reasoner", "o1", "o3", "max", "r1"}
_STRONG_PREFIXES = ("gpt-5", "grok-4", "deepseek-r")
_FAST_WORDS = {"haiku", "mini", "nano", "flash", "lite", "small", "instant", "tiny"}


def _billions(token: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)b", token)
    return float(m.group(1)) if m else None


def _key(model: str) -> str:
    return model.lower().rsplit("/", 1)[-1]


def name_tier(model: str) -> Literal["strong", "balanced", "fast"]:
    """Capability class from a model's name alone."""
    name = _key(model)
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


def profile_for(model: str) -> ModelProfile:
    """The competence profile of ``model``: configured or catalogued, else guessed from its name."""
    known = _PROFILES.get(model) or _PROFILES.get(_key(model))
    if known is not None:
        return known
    base = _TIER_PROFILES[name_tier(model)]
    sizes = [b for b in map(_billions, re.split(r"[-_.:/ ]+", _key(model))) if b is not None]
    if not sizes:
        return base
    # Open-weight models: a little more competence with size, a little less speed.
    bump = max(-8.0, min(8.0, (max(sizes) - 70) / 40))
    return ModelProfile(
        base.reasoning + bump,
        base.coding + bump,
        base.writing + bump,
        max(20.0, base.speed - bump),
        "heuristic",
    )


def price_for(model: str, *, auth: str = "api_key", self_hosted: bool = False) -> CallPrice:
    """What one token of ``model`` costs this owner on this route (USD per Mtok)."""
    if auth == "account":
        return CallPrice(0.0, 0.0, "subscription")
    if self_hosted:
        return CallPrice(0.0, 0.0, "self_hosted")
    listed = get_price(model)
    if listed is not None:
        return CallPrice(listed.input, listed.output, "listed")
    ref = _REFERENCE_PRICES.get(model) or _REFERENCE_PRICES.get(_key(model))
    if ref is not None:
        return CallPrice(ref[0], ref[1], "reference")
    tier_in, tier_out = _TIER_PRICES[name_tier(model)]
    return CallPrice(tier_in, tier_out, "estimated")


def register_profile(model: str, data: dict[str, float]) -> None:
    """Add or override a model's competence and/or reference price (``ARGUS_MODEL_PROFILES``)."""
    dims = {d: float(data[d]) for d in DIMENSIONS if d in data}
    if dims:
        base = _PROFILES.get(model) or profile_for(model)
        merged = {d: dims.get(d, getattr(base, d)) for d in DIMENSIONS}
        clamped = {k: max(0.0, min(100.0, v)) for k, v in merged.items()}
        _PROFILES[model] = ModelProfile(**clamped, source="configured")
    if "input" in data and "output" in data:
        _REFERENCE_PRICES[model] = (float(data["input"]), float(data["output"]))
