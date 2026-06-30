"""Cost stub — pricing a Usage and estimating a batch (invariant 5)."""

from __future__ import annotations

from sqbyl_runtime.cost import estimate_cost, price_usage, rate_for
from sqbyl_runtime.llm.base import Usage


def test_price_usage_includes_cache_tokens() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    rate = rate_for("claude-opus-4-8")
    expected = rate.input + rate.output + rate.cache_write + rate.cache_read
    assert price_usage(usage, "claude-opus-4-8") == expected


def test_unknown_model_falls_back_to_flagship_not_zero() -> None:
    usage = Usage(input_tokens=1_000_000)
    assert price_usage(usage, "some-future-model") == rate_for("claude-opus-4-8").input
    assert price_usage(usage, "some-future-model") > 0


def test_estimate_scales_with_calls() -> None:
    one = estimate_cost(
        model="claude-opus-4-8", calls=1, avg_input_tokens=1500, avg_output_tokens=400
    )
    ten = estimate_cost(
        model="claude-opus-4-8", calls=10, avg_input_tokens=1500, avg_output_tokens=400
    )
    assert one > 0
    assert ten == one * 10


def test_cheaper_model_estimates_less() -> None:
    kw = {"calls": 5, "avg_input_tokens": 1500, "avg_output_tokens": 400}
    assert estimate_cost(model="claude-haiku-4-5-20251001", **kw) < estimate_cost(
        model="claude-opus-4-8", **kw
    )
