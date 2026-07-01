"""Eval run reports — the per-run aggregate persisted to ``.sqbyl/runs/`` (spec §7, plan 3.3).

A :class:`ScoredRun` is the **source of the quality KPIs** the §7.5 reporting surface
(Phase 7.3) rolls up — so its shape is the run aggregate, not a bespoke log. Aggregates
(accuracy, % manual-review, self-repair rate) are computed from the per-question
:class:`QuestionResult` list so there is a single source of truth and no chance of a
stored aggregate drifting from the rows it summarizes.

These are dev-only models: benchmarks and their scored runs never ship in a release
(the release carries a :class:`~sqbyl_runtime.models.Scorecard`), so they live in the
``sqbyl`` package, which depends on ``sqbyl_runtime`` — never the reverse (invariant 1).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from sqbyl.models.judges import JudgeVerdict
from sqbyl_runtime.llm.base import Usage
from sqbyl_runtime.models import SqbylModel

# Layer-1 deterministic scorer names (spec §7). Stable strings so reports and the
# console can key off them without importing the scorer functions.
SCORER_SYNTAX_VALIDITY = "syntax_validity"
SCORER_SCHEMA_ACCURACY = "schema_accuracy"
SCORER_ASSET_ROUTING = "asset_routing"
SCORER_RESULT_CORRECTNESS = "result_correctness"


class Verdict(StrEnum):
    """The outcome of record for one question — always the *deterministic* Layer-1 call.

    Layer 1 emits only three of these: a result-set *mismatch* is **not** proof of
    incorrectness — different SQL can be semantically equivalent — so Layer 1 never asserts
    ``incorrect``. A mismatch (or a question with no executable gold) is routed to
    ``manual_review`` and stays there in the headline until a **human** resolves it.

    The Layer-2 judge never sets this field: it is *advisory* (it triages the review pile
    and explains why, via :attr:`QuestionResult.judge_suggestion`) and must not move the
    reported accuracy, which is the deterministic truth users report upstream. ``incorrect``
    is therefore never emitted by scoring; it exists only as a judge *suggestion* value and,
    later, a human-confirmed verdict (Phase 5.2).
    """

    correct = "correct"  # deterministic: generated result set matched the gold
    incorrect = "incorrect"  # never set by scoring — a judge suggestion / human verdict only
    manual_review = "manual_review"  # mismatch or no gold — awaits a human
    error = "error"  # the agent produced no executable SQL


class ScorerResult(SqbylModel):
    """One scorer's verdict for one question. ``passed=None`` means *not applicable*."""

    name: str
    passed: bool | None
    detail: str | None = None


class QuestionResult(SqbylModel):
    """The scored outcome of running one benchmark question through ``ask()``."""

    id: str
    question: str
    verdict: Verdict  # the Layer-1 verdict; preserved even after Layer 2 runs
    generated_sql: str
    plan: str = ""
    gold_sql: str | None = None
    gold_asset: str | None = None
    scorers: list[ScorerResult] = Field(default_factory=list)
    # Layer 2 (spec §7) is ADVISORY: the judge panel's per-dimension verdicts (each with a
    # rationale that explains *why the row needs review*) and the arbiter's suggested
    # disposition. ``judge_suggestion`` is a hint to speed a human's review — it never feeds
    # the headline accuracy (that stays deterministic). ``None`` means the panel didn't run
    # (the row was already resolved by Layer 1, or judging was off).
    judge_verdicts: list[JudgeVerdict] = Field(default_factory=list)
    judge_suggestion: Verdict | None = None
    used_assets: list[str] = Field(default_factory=list)
    selected_tables: list[str] = Field(default_factory=list)
    attempts: int = 0
    repaired: bool = False
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)  # agent tokens only
    cost_usd: float = 0.0  # agent cost only
    # Layer-2 spend is kept separate so per-role accounting (invariant 5, §7.5) never
    # mis-attributes judge tokens to the agent role/model.
    judge_usage: Usage = Field(default_factory=Usage)
    judge_cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace_id: str = ""

    @property
    def correct(self) -> bool:
        """Deterministically correct — the only thing that counts toward accuracy."""
        return self.verdict is Verdict.correct

    @property
    def needs_review(self) -> bool:
        return self.verdict is Verdict.manual_review

    @property
    def judged(self) -> bool:
        """True once the advisory judge panel has triaged this row (it made a suggestion)."""
        return self.judge_suggestion is not None

    def scorer(self, name: str) -> ScorerResult | None:
        """The recorded result for a named scorer, if it ran."""
        return next((s for s in self.scorers if s.name == name), None)


class ScoredRun(SqbylModel):
    """One eval run over a benchmark split — the per-run aggregate (spec §7).

    Stamped with the model per role that ran, so a score is never divorced from the model
    that produced it (spec §7/§11). Today only the ``agent`` role runs, so ``models`` is
    ``{"agent": ...}``; Layer-2 judges (Phase 5) will add their own entry. Also stamped
    with the ``as_of`` used to normalize ``now()``-relative gold — **pin it** (via
    ``sqbyl eval --as-of``) for a run to be reproducible across time; left unset it
    defaults to run start, which is internally consistent but drifts between runs.
    Reported **separately for dev and the held-out test set** — never conflated.
    """

    run_id: str
    split: str  # "dev" | "test" (the benchmark split this run scored)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    models: dict[str, str] = Field(default_factory=dict)
    as_of: datetime | None = None
    results: list[QuestionResult] = Field(default_factory=list)

    # --- aggregates: computed from results so they can never drift (the quality KPIs
    #     the §7.5 report layer reads) -------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def n_correct(self) -> int:
        return sum(1 for r in self.results if r.verdict is Verdict.correct)

    @property
    def n_manual_review(self) -> int:
        return sum(1 for r in self.results if r.verdict is Verdict.manual_review)

    @property
    def n_error(self) -> int:
        return sum(1 for r in self.results if r.verdict is Verdict.error)

    def n_suggested(self, suggestion: Verdict) -> int:
        """How many review-pile rows the advisory judge triaged with a given suggestion.

        A reporting aid only — these counts describe the ``manual_review`` queue (how much
        the judge thinks is likely-equivalent vs likely-wrong vs genuinely ambiguous), and
        never enter :attr:`accuracy`."""
        return sum(1 for r in self.results if r.judge_suggestion is suggestion)

    @property
    def accuracy(self) -> float:
        """Headline accuracy: fraction scored ``correct`` by the deterministic layer."""
        return self.n_correct / self.total if self.total else 0.0

    def accuracy_ci(self, *, z: float = 1.96) -> tuple[float, float]:
        """A Wilson score interval for ``accuracy`` (95% at the default ``z``).

        On the tens-of-questions eval sets sqbyl targets, a one- or two-question flip is
        often within run-to-run noise; the interval keeps a headline percentage honest
        about how much it can be trusted (spec §7.5). Returns ``(0, 0)`` for an empty run.
        """
        n = self.total
        if n == 0:
            return (0.0, 0.0)
        p = self.accuracy
        denom = 1.0 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
        return (max(0.0, center - margin), min(1.0, center + margin))

    @property
    def manual_review_rate(self) -> float:
        return self.n_manual_review / self.total if self.total else 0.0

    @property
    def self_repair_rate(self) -> float:
        """Fraction of answers that needed a retry — a leading indicator of brittle
        context (spec §7.5)."""
        if not self.total:
            return 0.0
        return sum(1 for r in self.results if r.repaired) / self.total

    @property
    def mean_latency_ms(self) -> float:
        return sum(r.latency_ms for r in self.results) / self.total if self.total else 0.0

    @property
    def total_usage(self) -> Usage:
        """All tokens spent this run — agent generation *and* Layer-2 judging."""
        total = Usage()
        for r in self.results:
            total = total + r.usage + r.judge_usage
        return total

    @property
    def total_tokens(self) -> int:
        return self.total_usage.total_tokens

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd + r.judge_cost_usd for r in self.results)

    def correct_ids(self) -> set[str]:
        return {r.id for r in self.results if r.verdict is Verdict.correct}


class RunDiff(SqbylModel):
    """Which questions a change fixed or broke between two runs (regression detection).

    Computed on the ``correct`` boolean: ``fixed`` flipped not-correct → correct,
    ``regressed`` flipped correct → not-correct. ``fixed`` + ``regressed`` are the
    flipped questions (spec §7).
    """

    from_run_id: str
    to_run_id: str
    fixed: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)
    still_passing: list[str] = Field(default_factory=list)
    still_failing: list[str] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)

    @property
    def flipped(self) -> list[str]:
        return sorted(self.fixed + self.regressed)


class OverfittingSignal(SqbylModel):
    """The dev↔test accuracy gap surfaced as a first-class overfitting signal (spec §7).

    A large positive gap (dev far above held-out test) means the loop has tuned to the
    iteration set rather than generalized — the thing the dev/test boundary (§7, plan
    3.4) exists to catch.
    """

    dev_accuracy: float
    test_accuracy: float
    threshold: float = 0.1

    @property
    def gap(self) -> float:
        return self.dev_accuracy - self.test_accuracy

    @property
    def overfit(self) -> bool:
        return self.gap > self.threshold
