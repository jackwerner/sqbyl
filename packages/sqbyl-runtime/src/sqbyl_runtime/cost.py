"""Token pricing + cost estimation (invariant 5).

Every paid command meters its spend to ``.sqbyl/usage.db`` and prints an up-front
estimate. This module is the single place that turns a :class:`Usage` (or a planned
call count) into dollars. The full estimate-before / budget / spend-meter machinery
is Phase 7; this is the stub paid commands route through *now* so the wiring exists
from the day each command is written, not retrofitted later.

Rates are list prices in USD per **million** tokens. They are approximate and easy
to update; treat them as estimates, not invoices.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqbyl_runtime.llm.base import Usage


@dataclass(frozen=True)
class ModelRate:
    """USD per million tokens for one model."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# Approximate Anthropic list prices (USD / 1M tokens). Update as pricing changes.
MODEL_RATES: dict[str, ModelRate] = {
    "claude-opus-4-8": ModelRate(input=15.0, output=75.0, cache_write=18.75, cache_read=1.5),
    "claude-sonnet-4-6": ModelRate(input=3.0, output=15.0, cache_write=3.75, cache_read=0.3),
    "claude-haiku-4-5-20251001": ModelRate(input=1.0, output=5.0, cache_write=1.25, cache_read=0.1),
}
# Fallback when a model id isn't in the table — priced as the flagship so estimates
# never silently read as $0.
_DEFAULT_RATE = MODEL_RATES["claude-opus-4-8"]


def rate_for(model: str) -> ModelRate:
    return MODEL_RATES.get(model, _DEFAULT_RATE)


def price_usage(usage: Usage, model: str) -> float:
    """Dollar cost of one metered call's :class:`Usage`."""
    rate = rate_for(model)
    return (
        usage.input_tokens * rate.input
        + usage.output_tokens * rate.output
        + usage.cache_creation_input_tokens * rate.cache_write
        + usage.cache_read_input_tokens * rate.cache_read
    ) / 1_000_000.0


def estimate_cost(
    *,
    model: str,
    calls: int,
    avg_input_tokens: int,
    avg_output_tokens: int,
) -> float:
    """A rough up-front estimate for a planned batch of ``calls`` (no cache assumed)."""
    rate = rate_for(model)
    per_call = (avg_input_tokens * rate.input + avg_output_tokens * rate.output) / 1_000_000.0
    return per_call * calls
