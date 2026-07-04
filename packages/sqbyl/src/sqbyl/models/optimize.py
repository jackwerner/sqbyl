"""The autonomous optimizer's result models (spec §6.C, plan 8.3).

``sqbyl optimize`` runs ``coach → apply → eval`` in a loop **against the dev set**, keeping
edits that raise the dev score and reverting those that don't, until a target accuracy or a
budget is hit. It returns a **frontier** of project versions — each a real, readable working-
tree diff — with their dev accuracy/cost/latency, so a human picks one. The held-out test is
scored **once**, on the picked version, and a large dev↔test gap is surfaced as an
overfitting warning rather than hidden (invariant 3: the loop never optimizes on test).

These are pydantic models (invariant 2); the loop lives in :mod:`sqbyl.optimize`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from sqbyl.models.runs import ScoredRun
from sqbyl_runtime.models import SqbylModel


class StopReason(StrEnum):
    """Why the loop halted — reported so the run is legible, not a black box."""

    target_met = "target_met"  # dev accuracy reached --target
    budget_exhausted = "budget_exhausted"  # the next paid step wouldn't fit --budget
    converged = "converged"  # a full round proposed nothing that helped
    no_proposals = "no_proposals"  # the Coach had nothing left to suggest
    max_rounds = "max_rounds"  # the safety cap on rounds was reached


class FrontierPoint(SqbylModel):
    """One version on the accuracy/cost/latency frontier — the baseline, or an accepted edit.

    Each accepted point strictly improved the dev score over the previous best (keep-if-it-
    helped), so the frontier is monotonic in ``dev_accuracy``; a human trades it off against
    ``dev_cost_usd`` / ``mean_latency_ms``. The applied edit is named (title + file + layer) so
    the point is a readable change, and ``git diff`` shows the cumulative working-tree edits.
    """

    version: int = Field(ge=0, description="0 is the baseline; each accepted edit increments it.")
    dev_accuracy: float = Field(ge=0.0, le=1.0)
    dev_accuracy_low: float = Field(ge=0.0, le=1.0)
    dev_accuracy_high: float = Field(ge=0.0, le=1.0)
    dev_n: int = Field(ge=0)
    dev_cost_usd: float = Field(ge=0.0)
    mean_latency_ms: float = Field(ge=0.0)
    # Net questions this version fixed over the previous best (fixed − broke), and whether that
    # paired gain clears a sign test — so a step accepted on a likely-noise flip is *visible*,
    # not silently ratcheted into the frontier (ml-systems). 0/False on the baseline.
    net_gain: int = 0
    significant: bool = False
    # The accepted proposal that produced this version (None on the baseline).
    proposal_title: str | None = None
    proposal_id: str | None = None
    target_file: str | None = None
    layer: str | None = None

    @classmethod
    def from_run(
        cls,
        run: ScoredRun,
        *,
        version: int,
        net_gain: int = 0,
        significant: bool = False,
        proposal_title: str | None = None,
        proposal_id: str | None = None,
        target_file: str | None = None,
        layer: str | None = None,
    ) -> FrontierPoint:
        low, high = run.accuracy_ci()
        return cls(
            version=version,
            dev_accuracy=run.accuracy,
            dev_accuracy_low=low,
            dev_accuracy_high=high,
            dev_n=run.total,
            dev_cost_usd=run.total_cost_usd,
            mean_latency_ms=run.mean_latency_ms,
            net_gain=net_gain,
            significant=significant,
            proposal_title=proposal_title,
            proposal_id=proposal_id,
            target_file=target_file,
            layer=layer,
        )


class OptimizeResult(SqbylModel):
    """The outcome of an optimize run — the frontier, the pick, and the one held-out score."""

    frontier: list[FrontierPoint] = Field(default_factory=list)
    picked: int = Field(default=0, description="Index into `frontier` of the shipped version.")
    stopped: StopReason
    rounds: int = Field(default=0, ge=0)
    spent_usd: float = Field(default=0.0, ge=0.0)
    target: float = Field(ge=0.0, le=1.0)
    # Which models produced these numbers — a score divorced from its model can't be trusted
    # across a model bump (spec §7/§11). e.g. {"agent": "claude-opus-4-8", "coach": ...}.
    models: dict[str, str] = Field(default_factory=dict)
    # Whether the picked version's cumulative dev gain over baseline clears a paired sign test.
    # False (with a real gain) means "the dev improvement is within noise" — the CLI says so and
    # defers to the held-out number, rather than presenting an optimized point as settled.
    picked_significant: bool = False
    # What the loop actually did to the working tree — so reverted edits aren't invisible.
    edits_tried: int = Field(default=0, ge=0)
    edits_kept: int = Field(default=0, ge=0)
    edits_reverted: int = Field(default=0, ge=0)
    # The held-out test scored ONCE on the picked version (None if scoring was skipped).
    test_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    test_n: int | None = Field(default=None, ge=0)
    # picked dev accuracy − held-out test accuracy: the overfitting signal (spec §7/§11).
    dev_test_gap: float | None = None

    @property
    def picked_point(self) -> FrontierPoint:
        return self.frontier[self.picked]

    @property
    def improved(self) -> float:
        """Dev accuracy gained from baseline to the picked version."""
        if not self.frontier:
            return 0.0
        return self.picked_point.dev_accuracy - self.frontier[0].dev_accuracy
