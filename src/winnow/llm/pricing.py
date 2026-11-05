"""Per-model pricing in USD per 1M tokens. Sources: PLAN.md §2 (promptfoo, 27 Oct 2026)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input_per_1m: float
    cached_per_1m: float
    output_per_1m: float


PRICES: dict[str, ModelPrice] = {
    "muse-spark-1.3": ModelPrice(input_per_1m=1.25, cached_per_1m=0.15, output_per_1m=4.25),
    "muse-spark-1.3-contributor": ModelPrice(
        input_per_1m=0.10, cached_per_1m=0.10, output_per_1m=0.20
    ),
}


def get_price(model: str) -> ModelPrice:
    """Unknown models fall back to standard pricing so cost is never silently zero."""
    return PRICES.get(model, PRICES["muse-spark-1.3"])


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Cached tokens are a subset of input tokens, billed at the cached rate."""
    price = get_price(model)
    uncached = max(0, input_tokens - cached_tokens)
    return (
        uncached * price.input_per_1m + cached_tokens * price.cached_per_1m
    ) / 1_000_000 + output_tokens * price.output_per_1m / 1_000_000
