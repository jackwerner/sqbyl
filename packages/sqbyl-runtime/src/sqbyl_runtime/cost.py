"""Token pricing + cost estimation + the live spend meter (invariant 5).

This module is the single place that turns a :class:`Usage` (or a planned call
count) into dollars, and the single place that *tracks* dollars as they're spent:

  * :func:`price_usage` / :func:`estimate_cost` — the pricing primitives.
  * :class:`CostEstimate` — the structured up-front estimate a paid command prints
    (and ``--dry-run`` returns without spending a cent).
  * :class:`SpendMeter` — the live tally a command runs work against: it meters every
    call to ``.sqbyl/usage.db`` and enforces a ``--budget`` cap (hard-stop in ``--auto``,
    pause-and-ask in guided mode via the CLI).

Rates are list prices in USD per **million** tokens. They are approximate and easy
to update; treat them as estimates, not invoices.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from sqbyl_runtime.llm.base import Usage

if TYPE_CHECKING:
    from sqbyl_runtime.state.usage import UsageStore

# Costs are floats; a hair of tolerance keeps a cap from tripping on rounding noise
# (e.g. an estimate of exactly the budget must be allowed to run).
_EPSILON = 1e-9


@dataclass(frozen=True)
class ModelRate:
    """USD per million tokens for one model."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# Approximate list prices (USD / 1M tokens). Update as pricing changes.
# OpenAI caches automatically with no separate cache-*write* charge, so cache_write == input
# for those rows; cache_read is the discounted cached-input rate.
MODEL_RATES: dict[str, ModelRate] = {
    # Anthropic
    "claude-opus-4-8": ModelRate(input=15.0, output=75.0, cache_write=18.75, cache_read=1.5),
    "claude-sonnet-4-6": ModelRate(input=3.0, output=15.0, cache_write=3.75, cache_read=0.3),
    "claude-haiku-4-5-20251001": ModelRate(input=1.0, output=5.0, cache_write=1.25, cache_read=0.1),
    # OpenAI (gpt-5 family)
    "gpt-5": ModelRate(input=1.25, output=10.0, cache_write=1.25, cache_read=0.125),
    "gpt-5-mini": ModelRate(input=0.25, output=2.0, cache_write=0.25, cache_read=0.025),
    "gpt-5-nano": ModelRate(input=0.05, output=0.4, cache_write=0.05, cache_read=0.005),
}
# Fallback when a model id isn't in the table — priced as the flagship so estimates never
# silently read as $0. Note this same rate prices *actual* metered spend too, so a real call
# on an unrecognized (often cheaper) model is billed in usage.db as an upper bound, not an
# exact invoice — add the model to MODEL_RATES to reconcile it precisely.
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


# ── the structured up-front estimate (spec §5.5, §9) ────────────────────────────────────


class EstimateItem(BaseModel):
    """One planned step in a command's cost estimate — the rows of the SAM-style plan."""

    label: str
    model: str
    calls: int
    avg_input_tokens: int
    avg_output_tokens: int

    @property
    def cost_usd(self) -> float:
        return estimate_cost(
            model=self.model,
            calls=self.calls,
            avg_input_tokens=self.avg_input_tokens,
            avg_output_tokens=self.avg_output_tokens,
        )


class CostEstimate(BaseModel):
    """A planned batch of paid work, itemized. Printed before spending and returned by
    ``--dry-run`` / ``sqbyl cost`` — the "here's the plan and the estimate" of spec §5.5.

    It is an *estimate*, not an invoice: no prompt-cache savings are assumed, so the real
    metered spend should come in at or under it.
    """

    items: list[EstimateItem] = []

    @property
    def total_usd(self) -> float:
        return sum(item.cost_usd for item in self.items)

    @property
    def calls(self) -> int:
        return sum(item.calls for item in self.items)

    def render(self, *, indent: str = "  ") -> str:
        """The itemized plan as a right-aligned ``label … ~$cost`` table."""
        if not self.items:
            return f"{indent}(no paid work planned)"
        width = max(len(item.label) for item in self.items)
        lines = [
            f"{indent}{item.label.ljust(width)}   ~${item.cost_usd:.4f}  "
            f"({item.calls}× {item.model})"
            for item in self.items
        ]
        lines.append(f"{indent}{'─' * (width + 20)}")
        lines.append(f"{indent}{'estimated total'.ljust(width)}   ~${self.total_usd:.4f}")
        return "\n".join(lines)


# ── the live spend meter + budget cap (invariant 5) ─────────────────────────────────────


class BudgetError(RuntimeError):
    """A planned paid step would push spend past the hard cap.

    Raised by :meth:`SpendMeter.guard` in ``--auto``/hard mode so a headless run stops
    at the cap rather than silently overspending (spec §9). In guided mode the CLI checks
    :meth:`SpendMeter.would_exceed` first and pauses to ask instead.
    """

    def __init__(self, *, spent: float, budget: float, attempted: float) -> None:
        self.spent = spent
        self.budget = budget
        self.attempted = attempted
        super().__init__(
            f"budget ${budget:.4f} would be exceeded: ${spent:.4f} spent + "
            f"~${attempted:.4f} planned"
        )


class SpendMeter:
    """A running tally of paid spend, capped by an optional ``--budget`` (spec §9).

    One meter spans a whole command. Every call is priced, added to the tally, and — when a
    store is attached — durably recorded to ``.sqbyl/usage.db``, so the ledger total always
    reconciles with the meter exactly. The cap is advisory via :meth:`would_exceed` (for a
    guided pause) and enforced via :meth:`guard` (a hard stop that raises :class:`BudgetError`).

    **Concurrency.** The lock makes each *field access* atomic — :meth:`record` and
    :attr:`spent` are safe to call from many threads and the ledger stays exact. It does
    **not** make the cap a concurrency-safe admission gate: :meth:`guard`/:meth:`would_exceed`
    and the later :meth:`record` are separate steps, so N threads can each pass the check
    before any records and collectively overshoot by up to N−1 calls. Serial callers (the CLI
    commands) are fine; a parallel fan-out must bound dispatch with the orchestrator's own
    pre-dispatch budget gate (Phase 6.1) rather than trusting :meth:`guard` alone.
    """

    def __init__(
        self,
        *,
        budget: float | None = None,
        store: UsageStore | None = None,
        command: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        self._budget = budget
        self._store = store
        self._command = command
        self._content_hash = content_hash
        self._spent = 0.0
        self._lock = threading.Lock()

    @property
    def budget(self) -> float | None:
        return self._budget

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> float | None:
        """Dollars left under the cap, or ``None`` when uncapped."""
        if self._budget is None:
            return None
        with self._lock:
            return self._budget - self._spent

    def would_exceed(self, next_cost: float) -> bool:
        """Would spending ``next_cost`` more push past the cap? (Never True when uncapped.)"""
        if self._budget is None:
            return False
        with self._lock:
            return self._spent + next_cost > self._budget + _EPSILON

    def guard(self, next_cost: float) -> None:
        """Hard-stop precondition: raise :class:`BudgetError` if the next step won't fit."""
        if self.would_exceed(next_cost):
            with self._lock:
                spent = self._spent
            assert self._budget is not None
            raise BudgetError(spent=spent, budget=self._budget, attempted=next_cost)

    def record(
        self,
        usage: Usage,
        *,
        model: str,
        role: str | None = None,
        run_id: str | None = None,
    ) -> float:
        """Price one completed call's ``usage``, add it to the tally, and persist it.

        Returns the call's dollar cost. This is metering *after* a call — the cap is
        enforced *before* dispatch via :meth:`guard`/:meth:`would_exceed`, so a recorded
        call can legitimately carry the tally over budget by one call's worth.
        """
        cost = price_usage(usage, model)
        with self._lock:
            self._spent += cost
            store = self._store
        if store is not None:
            from sqbyl_runtime.state.usage import UsageRecord

            store.record(
                UsageRecord.from_usage(
                    usage,
                    model=model,
                    command=self._command,
                    role=role,
                    cost_usd=cost,
                    run_id=run_id,
                    content_hash=self._content_hash,
                )
            )
        return cost
