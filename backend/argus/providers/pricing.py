"""Per-model token pricing (USD per million tokens) for cost accounting.

Only first-party prices we can vouch for ship by default; add or override any
model at runtime with ``register_pricing`` (wired to ``ARGUS_MODEL_PRICING``).
Unknown and self-hosted models cost 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from argus.providers.types import Usage


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


_PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0),
    "claude-fable-5": Price(10.0, 50.0),
    "claude-opus-5-5": Price(4.0, 20.0, cache_read=0.20),
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5-5": Price(2.0, 10.0, cache_read=0.20),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-5-5": Price(0.10, 0.50),
    "claude-haiku-4-5": Price(1.0, 5.0),
}


def register_pricing(model: str, input_per_mtok: float, output_per_mtok: float, **kw: float) -> None:
    _PRICES[model] = Price(input_per_mtok, output_per_mtok, kw.get("cache_read"), kw.get("cache_write"))


def get_price(model: str) -> Price | None:
    if model in _PRICES:
        return _PRICES[model]
    # Tolerate provider prefixes such as "anthropic/claude-opus-5-5" (OpenRouter).
    tail = model.rsplit("/", 1)[-1]
    return _PRICES.get(tail)


def estimate_cost(model: str, usage: Usage) -> float:
    price = get_price(model)
    if price is None:
        return 0.0
    cache_read = price.cache_read if price.cache_read is not None else price.input * 0.1
    cache_write = price.cache_write if price.cache_write is not None else price.input * 1.25
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * cache_write
    ) / 1_000_000
    return round(total, 8)
