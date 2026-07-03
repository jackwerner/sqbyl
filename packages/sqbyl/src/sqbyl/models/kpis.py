"""The operational KPI report — the numbers a team reports up (spec §7.5).

``KpiReport`` is a pydantic artifact like every other (invariant 2), rolled up from data
sqbyl **already** meters and stores — ``.sqbyl/usage.db`` (cost), ``.sqbyl/runs/`` (quality),
and per-query latencies — into the four KPI families. It is a *reporting view*, not new
collection: **aggregates only, never row data** (§13), and cost + quality are reported
**separately for dev and the held-out test set**, never conflated (spec §7).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from sqbyl_runtime.models import SqbylModel


class UnitEconomics(SqbylModel):
    """Finance's family: what each answered question actually costs (spec §7.5).

    These are the **agent's production** economics — the cost a released ``ask()`` incurs.
    Layer-2 judge spend is a *dev-only* cost (it doesn't run in production), so it is
    deliberately excluded from ``$/query`` here and folded into the report's lifetime total
    instead; ``judge_cost_per_query_usd`` surfaces it separately for the curator (invariant 5).
    """

    cost_per_query_usd: float = Field(ge=0)  # agent role only
    tokens_per_query: float = Field(ge=0)  # agent role only
    # Share of prompt input tokens served from the prompt cache (reads / (fresh input + reads)).
    # 0 when nothing was cached; the major cost lever on a repeated eval (spec §9).
    cache_hit_rate: float = Field(ge=0, le=1)
    judge_cost_per_query_usd: float = Field(ge=0, default=0.0)  # dev-only overhead, not production
    volume_per_month: int | None = None
    projected_monthly_usd: float | None = None  # agent $/query × volume — production run-rate


class QualityKpis(SqbylModel):
    """The curator's family, for one split (spec §7.5). ``accuracy`` is the deterministic
    floor — the judge never moves it — carried with its Wilson interval so a small eval set
    reads honestly."""

    split: str  # "dev" | "test"
    n: int
    accuracy: float = Field(ge=0, le=1)
    accuracy_low: float = Field(ge=0, le=1)
    accuracy_high: float = Field(ge=0, le=1)
    manual_review_rate: float = Field(ge=0, le=1)
    self_repair_rate: float = Field(ge=0, le=1)
    low_confidence: bool  # n below the small-sample floor — treat the point estimate as directional
    models: dict[str, str] = {}  # per-role model ids that scored THIS split (provenance, §7/§11)


class PerformanceKpis(SqbylModel):
    """SRE's family: per-query latency percentiles, from the same spans any OTel backend
    would chart (spec §7.5)."""

    n: int
    latency_p50_ms: float = Field(ge=0)
    latency_p95_ms: float = Field(ge=0)
    # Below the small-sample floor a "p95" is really just the worst one or two queries —
    # directional, not a stable tail. Flagged so an SRE doesn't chart it as settled.
    low_confidence: bool = False


class KpiReport(SqbylModel):
    """The rolled-up scorecard `sqbyl report` / `proj.kpis()` emits (spec §7.5).

    Dev and held-out test quality are separate fields — never merged into one accuracy —
    and ``dev_test_gap`` surfaces the overfitting signal as a first-class number. Everything
    here is an aggregate; no question text, SQL, or result rows appear (§13).
    """

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    models: dict[str, str] = {}  # the per-role model ids the measured dev run ran on (provenance)

    unit_economics: UnitEconomics
    dev_quality: QualityKpis | None = None
    test_quality: QualityKpis | None = None
    dev_test_gap: float | None = None  # dev.accuracy − test.accuracy; large + = overfit (§7)
    performance: PerformanceKpis | None = None

    # Process & readiness (spec §7.5): distance-to-ship and how many dev iterations it took.
    readiness_target: float = Field(ge=0, le=1)
    readiness_gap: float | None = None  # target − dev accuracy; ≤0 means the target is met
    round_trips_to_ship: int = Field(ge=0, default=0)  # dev eval runs recorded so far

    # Lifetime metered totals straight from usage.db (reconcile with the ledger).
    total_cost_usd: float = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @property
    def readiness_met(self) -> bool:
        """Target met **conservatively**: the dev accuracy's lower 95% bound clears the target,
        not just the point estimate — so a noisy one-question flip near the line doesn't read
        as "shipped" while the interval still straddles the target (spec §7.5)."""
        return (
            self.dev_quality is not None and self.dev_quality.accuracy_low >= self.readiness_target
        )
