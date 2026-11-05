"""Unit tests for pricing (PLAN §10 budget math). No network."""

from winnow.llm.pricing import cost_usd


def test_cost_without_cache_matches_plan_example() -> None:
    # PLAN §10: ~171k in + 20k out ≈ $0.30 without caching.
    cost = cost_usd("muse-spark-1.3", 171_000, 0, 20_000)
    assert 0.28 < cost < 0.32


def test_cached_input_is_cheaper() -> None:
    full = cost_usd("muse-spark-1.3", 10_000, 0, 0)
    half_cached = cost_usd("muse-spark-1.3", 10_000, 5_000, 0)
    assert half_cached < full
    assert half_cached == 5_000 * 1.25 / 1_000_000 + 5_000 * 0.15 / 1_000_000


def test_unknown_model_falls_back_to_standard_pricing() -> None:
    assert cost_usd("no-such-model", 1_000_000, 0, 0) == 1.25
