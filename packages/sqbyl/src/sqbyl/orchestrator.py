"""The orchestrator — bounded, rate-limit-aware parallel fan-out (spec §3 #8, plan 6.1).

After the free deterministic pass and the user's costed go-ahead, the enrichment work
(per-table annotation, join inference, synthesis, baseline eval, fix pre-computation) is a
bag of independent paid units. Running them one at a time is slow; running them naively in
parallel self-DoSes the API key on a 42-table schema. This module is the middle path:

- a **bounded worker pool** sized to the account's API tier;
- **retry-with-backoff on 429s** (the seam's :class:`RateLimitError`), honoring a server
  ``Retry-After`` hint when present, so a burst backs off instead of failing;
- **cache-priming**: the one unit that fills the shared prompt cache runs to completion
  *before* the parallel wave is released, so the rest read from cache instead of each
  paying the cache-write cost;
- **partial-failure tolerance**: a unit that raises becomes a failed :class:`UnitOutcome`
  (which the attention router, plan 6.2, turns into a low-confidence card) — never a hard
  stop that loses its siblings' work;
- **budget awareness** (invariant 5): a running spend total gates dispatch; once the cap is
  reached, remaining units are left ``skipped`` rather than silently overspending.

It is generic over closures — it knows nothing about annotation, synth, or the LLM seam
beyond :class:`RateLimitError`. Pricing is injected (invariant 1: the runtime cost table
isn't imported here), so the same engine drives any mix of paid work. This is dev-only
orchestration, so it lives in ``sqbyl``; the runtime stays minimal.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

from sqbyl_runtime.llm.base import RateLimitError, Usage

T = TypeVar("T")

# The engine prices a unit's usage to enforce ``--budget``; the caller closes over the model
# (invariant 1 keeps the runtime cost table out of this dev module).
PriceFn = Callable[[Usage], float]


@dataclass
class WorkProduct(Generic[T]):
    """What a unit's work returns: the artifact, its token usage, and an optional confidence.

    ``confidence`` (when the unit produces one) is what the attention router consumes to
    decide auto-apply vs. surface-for-review (plan 6.2)."""

    value: T
    usage: Usage = field(default_factory=Usage)
    confidence: float | None = None


@dataclass
class WorkUnit(Generic[T]):
    """One schedulable piece of approved paid work.

    ``run`` is a closure that performs the work and returns a :class:`WorkProduct`. It should
    raise :class:`RateLimitError` on a 429 (the seam does this) so the engine backs off and
    retries; any other exception is treated as a real failure and degraded to a card.

    ``primes_cache`` marks the single unit that fills the shared prompt cache. The engine runs
    every cache-priming unit to completion *before* releasing the parallel wave, so the wave
    reads the warm cache instead of each unit racing to write it."""

    id: str
    run: Callable[[], WorkProduct[T]]
    kind: str = ""
    label: str = ""
    primes_cache: bool = False


class UnitStatus(StrEnum):
    ok = "ok"
    failed = "failed"  # raised a non-rate-limit error, or exhausted retries on 429s
    skipped = "skipped"  # never run — the budget cap was already reached


@dataclass
class UnitOutcome(Generic[T]):
    """The result of running (or skipping) one unit — the router's input, plan 6.2.

    Contract for the attention router (plan 6.2): branch on ``status``. A ``failed`` outcome
    is the *absence* of a result (``value is None``, ``confidence is None``) — it needs a
    retry or a human, and must **not** be treated as a ``confidence == 0`` sample in the
    readiness/leverage math. A genuine low-confidence result is an ``ok`` outcome carrying a
    real ``value`` and a real (small) ``confidence``. Conflating the two would silently blend
    "we couldn't produce this" into the calibrated confidence distribution.

    ``usage`` is the authoritative metered total for this unit — it folds in tokens billed by
    retried attempts, so it can exceed ``product.usage`` (the final successful call alone). One
    honest gap: tokens burned *inside* a closure that then raises a non-rate-limit error are
    not observable here and go un-metered — the engine only sees what a :class:`WorkProduct`
    (on success) or a :class:`RateLimitError` (on a 429) carries."""

    unit: WorkUnit[T]
    status: UnitStatus
    product: WorkProduct[T] | None = None
    error: str | None = None
    attempts: int = 0
    usage: Usage = field(default_factory=Usage)

    @property
    def ok(self) -> bool:
        return self.status is UnitStatus.ok

    @property
    def value(self) -> T | None:
        return self.product.value if self.product is not None else None

    @property
    def confidence(self) -> float | None:
        return self.product.confidence if self.product is not None else None


class EventKind(StrEnum):
    """Progress events for the live checklist + spend meter (spec §3 #8)."""

    priming = "priming"  # a cache-priming unit is running (before the wave)
    started = "started"
    retrying = "retrying"  # a 429 backoff is in progress
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"  # budget reached; unit left for a later run


@dataclass
class OrchestratorEvent:
    """One progress tick. ``spent_usd`` is the running metered total after this event."""

    kind: EventKind
    unit_id: str
    label: str = ""
    attempt: int = 0
    detail: str = ""
    spent_usd: float = 0.0


@dataclass
class OrchestratorResult(Generic[T]):
    """Every unit's outcome plus the summed usage — order matches the input units."""

    outcomes: list[UnitOutcome[T]]
    total_usage: Usage
    total_cost_usd: float = 0.0

    @property
    def ok(self) -> list[UnitOutcome[T]]:
        return [o for o in self.outcomes if o.status is UnitStatus.ok]

    @property
    def failures(self) -> list[UnitOutcome[T]]:
        return [o for o in self.outcomes if o.status is UnitStatus.failed]

    @property
    def skipped(self) -> list[UnitOutcome[T]]:
        return [o for o in self.outcomes if o.status is UnitStatus.skipped]


class Orchestrator:
    """A bounded, rate-limit-aware, budget-capped parallel runner for paid work units.

    Network-free by construction: it only invokes the units' closures and reacts to
    :class:`RateLimitError`. ``sleep`` and ``rng`` are injectable, so a test can make the whole
    run deterministic; in production the backoff jitter is intentionally random (thundering-herd
    avoidance), so a live run's retry timing is *not* reproducible.

    Backoff de-correlates each unit's *first* retry, but the pool refills to ``concurrency`` and
    re-fires at full width, so a genuinely tier-saturated key can oscillate (storm → backoff →
    storm) rather than settle. Adaptive wave-width (AIMD on sustained 429s) is a Phase 7
    follow-up; today's posture is honest-but-fixed concurrency.
    """

    def __init__(
        self,
        *,
        concurrency: int = 4,
        max_retries: int = 4,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        on_event: Callable[[OrchestratorEvent], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._on_event = on_event
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = threading.Lock()  # guards event emission + the running spend total
        self._spent = 0.0  # running metered total for the in-progress run() call

    def run(
        self,
        units: Sequence[WorkUnit[T]],
        *,
        budget: float | None = None,
        price: PriceFn | None = None,
    ) -> OrchestratorResult[T]:
        """Run ``units`` — cache-priming first (serially), then the rest in a bounded wave.

        ``budget`` caps cumulative metered spend (needs ``price`` to meter): once reached, not-
        yet-dispatched units are left ``skipped``. Because up to ``concurrency`` units may be
        in flight when the cap trips, the stop is best-effort to within one wave — the honest
        posture until the Phase 7 estimate/pause machinery lands.
        """
        if len({u.id for u in units}) != len(units):
            # Outcomes are keyed and re-ordered by id; duplicates would silently collapse
            # (double-counting usage, breaking the input-order contract).
            raise ValueError("WorkUnit ids must be unique")
        outcomes: dict[str, UnitOutcome[T]] = {}
        self._spent = 0.0

        priming = [u for u in units if u.primes_cache]
        wave = [u for u in units if not u.primes_cache]

        # Cache-priming units run to completion first so the wave reads a warm cache.
        for unit in priming:
            self._emit(EventKind.priming, unit)
            outcomes[unit.id] = self._exec(unit)
            self._account(outcomes[unit.id], price)

        self._run_wave(wave, outcomes, budget=budget, price=price)

        ordered = [outcomes[u.id] for u in units]
        total = Usage()
        for o in ordered:
            total = total + o.usage
        return OrchestratorResult(outcomes=ordered, total_usage=total, total_cost_usd=self._spent)

    def _run_wave(
        self,
        wave: Sequence[WorkUnit[T]],
        outcomes: dict[str, UnitOutcome[T]],
        *,
        budget: float | None,
        price: PriceFn | None,
    ) -> None:
        """Dispatch the wave across the pool, checking the budget before each new submit."""
        if not wave:
            return
        queue = list(wave)
        idx = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            in_flight: dict[Future[UnitOutcome[T]], WorkUnit[T]] = {}

            def fill() -> None:
                nonlocal idx
                while len(in_flight) < self.concurrency and idx < len(queue):
                    unit = queue[idx]
                    idx += 1
                    if self._over_budget(budget, price):
                        outcomes[unit.id] = UnitOutcome(unit, UnitStatus.skipped)
                        self._emit(EventKind.skipped, unit, detail="budget reached")
                    else:
                        in_flight[pool.submit(self._exec, unit)] = unit

            fill()
            while in_flight:
                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for fut in done:
                    unit = in_flight.pop(fut)
                    outcome = fut.result()
                    outcomes[unit.id] = outcome
                    self._account(outcome, price)
                fill()

    def _over_budget(self, budget: float | None, price: PriceFn | None) -> bool:
        if budget is None or price is None:
            return False
        with self._lock:
            return self._spent >= budget

    def _account(self, outcome: UnitOutcome[T], price: PriceFn | None) -> None:
        """Add a completed unit's metered cost to the running total (thread-safe)."""
        if price is None:
            return
        cost = price(outcome.usage)
        with self._lock:
            self._spent += cost

    def _exec(self, unit: WorkUnit[T]) -> UnitOutcome[T]:
        """Run one unit with retry-on-429 backoff; any other error degrades to a card.

        ``burned`` accumulates tokens billed across *all* attempts (a rejected 429 usually
        bills nothing, but if it carries usage we meter it) so a retried unit reports its true
        spend, not just the final call's (invariant 5)."""
        attempt = 0
        burned = Usage()
        while True:
            attempt += 1
            try:
                self._emit(EventKind.started, unit, attempt=attempt)
                product = unit.run()
                burned = burned + product.usage
                self._emit(EventKind.succeeded, unit, attempt=attempt)
                return UnitOutcome(
                    unit, UnitStatus.ok, product=product, attempts=attempt, usage=burned
                )
            except RateLimitError as exc:
                if exc.usage is not None:
                    burned = burned + exc.usage
                if attempt > self.max_retries:
                    detail = "rate-limited: retries exhausted"
                    self._emit(EventKind.failed, unit, attempt=attempt, detail=detail)
                    return UnitOutcome(
                        unit, UnitStatus.failed, error=detail, attempts=attempt, usage=burned
                    )
                delay = self._backoff(attempt, exc.retry_after)
                self._emit(
                    EventKind.retrying, unit, attempt=attempt, detail=f"429; backoff {delay:.2f}s"
                )
                self._sleep(delay)
            except Exception as exc:  # partial-failure tolerance: degrade, don't abort siblings
                detail = f"{type(exc).__name__}: {exc}"
                self._emit(EventKind.failed, unit, attempt=attempt, detail=detail)
                return UnitOutcome(
                    unit, UnitStatus.failed, error=detail, attempts=attempt, usage=burned
                )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Server ``Retry-After`` wins; else exponential with full jitter, capped."""
        if retry_after is not None:
            return min(retry_after, self.max_backoff)
        ceiling = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)))
        return self._rng.uniform(0.0, ceiling)

    def _emit(
        self,
        kind: EventKind,
        unit: WorkUnit[T],
        *,
        attempt: int = 0,
        detail: str = "",
    ) -> None:
        if self._on_event is None:
            return
        with self._lock:
            spent = self._spent
        self._on_event(
            OrchestratorEvent(
                kind=kind,
                unit_id=unit.id,
                label=unit.label or unit.id,
                attempt=attempt,
                detail=detail,
                spent_usd=spent,
            )
        )
