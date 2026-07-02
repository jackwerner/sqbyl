"""Attention-router models — decisions, the queue, and the readiness signal (spec §3 #9, §5.5).

The router's job is to spend the human's attention only where it's scarce. Every machine-made
decision carries a **confidence**; the high-confidence ones are auto-applied (with one-click
undo) and the rest are surfaced into a **single queue sorted by leverage** — the fewest
decisions that move readiness the most. On top sits the **readiness signal**: how far the
agent is from shippable and how many decisions close the gap ("86% · 6 decisions to 96%").

These are dev-only review-surface models (they describe the console queue, not a release), so
they live in ``sqbyl``. ``leverage`` here is an *estimate* the producer supplies (like the
Coach's ``predicted_fixes``), not a measured quantity — the readiness math is honest about
that, and the interval on :class:`ReadinessSignal` keeps the headline from over-claiming on a
small eval set.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from sqbyl_runtime.models import SqbylModel


class DecisionKind(StrEnum):
    """What kind of decision a card represents — drives its icon/grouping in the console."""

    business_meaning = "business_meaning"  # only a human can define it (spec §5.5 ①)
    measure = "measure"
    synonym = "synonym"
    named_filter = "named_filter"
    column_description = "column_description"
    table_description = "table_description"
    example = "example"
    instruction = "instruction"  # global prose — last resort (mirrors CoachLayer)
    judge_review = "judge_review"  # a review-pile row awaiting a human ruling
    blocked = "blocked"  # a unit that failed to produce — needs a retry or a human


class Decision(SqbylModel):
    """One machine-made decision — a *decision with a default*, never an open question (§5.5).

    ``suggestion`` is the pre-filled answer a reviewer accepts / edits / rejects. ``confidence``
    is how sure the machine is; ``leverage`` is the estimated readiness gain (accuracy points,
    ``0..1``) if accepted — an *estimate*, not a measurement. ``requires_human`` forces the card
    to the queue no matter how confident the machine is: business-meaning definitions, judge
    rulings, and blocked units are the human's call by nature (spec §1.5, §5.5)."""

    id: str
    kind: DecisionKind
    title: str
    detail: str = ""
    suggestion: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    leverage: float = Field(default=0.0, ge=0.0)
    affected_questions: list[str] = Field(default_factory=list)
    source: str = ""  # the producing unit/tool, e.g. "coach:p1", "annotate:orders"
    target_file: str = ""  # the applyable target, when the decision maps to a file edit
    requires_human: bool = False

    @property
    def auto_applyable(self) -> bool:
        """Only non-human-required decisions can be auto-applied (confidence gate is separate)."""
        return not self.requires_human


class ReadinessSignal(SqbylModel):
    """Distance-to-shippable + how many decisions close it (spec §5.5).

    Two different kinds of number live here, and the distinction is load-bearing:

    - ``accuracy`` is a **measured** deterministic headline, with a Wilson interval
      ``[accuracy_low, accuracy_high]`` so it never reads as more precise than a small eval
      set supports (spec §7.5).
    - ``decisions_to_target`` and ``projected`` are a **projection**, not a measurement: they
      sum the *estimated* leverage the producers (e.g. the Coach's ``predicted_fixes``) claim
      for each decision. Leverage is unverified until a re-eval actually lands the edits, so
      these are shown with a ``~`` and the word "projected" — a plan, not a promise.
      ``reachable`` is False when even the whole queue can't estimate its way to ``target``.

    Overlap is de-duplicated (a question two decisions both claim can't be fixed twice), but no
    interval is attached to the projection itself — calibrating that needs the realized-vs-
    predicted fix-rate history the Coach doesn't record yet (deferred, see the Phase 8 note in
    ``coach``). Until then the honest posture is: label it an estimate, don't dress it up."""

    accuracy: float
    target: float
    n: int  # eval sample size — drives the small-N caveat
    accuracy_low: float = 0.0
    accuracy_high: float = 0.0
    decisions_to_target: int = 0
    projected: float = 0.0
    reachable: bool = True

    @property
    def reached(self) -> bool:
        return self.accuracy >= self.target

    @property
    def low_confidence(self) -> bool:
        """The eval set is too small to trust the *measured* headline as a point estimate
        (§7.5). Note this speaks only to ``accuracy``; the projection is *always* an estimate,
        small-N or not — never read ``low_confidence == False`` as "the projection is solid.\""""
        from sqbyl.stats import SMALL_N_FLOOR

        return self.n < SMALL_N_FLOOR

    def headline(self) -> str:
        """The one-line meter, e.g. ``"86% · ~6 decisions to 96% (projected)"`` (spec §5.5).

        The ``~`` and "(projected)" keep the estimate honest: the left number is measured, the
        right is a plan built on unverified leverage."""
        pct = f"{self.accuracy * 100:.0f}%"
        target_pct = f"{self.target * 100:.0f}%"
        if self.reached:
            return f"{pct} · target {target_pct} reached"
        if not self.reachable:
            proj = f"{self.projected * 100:.0f}%"
            return f"{pct} · {target_pct} not reachable with queued decisions (best ~{proj})"
        n = self.decisions_to_target
        word = "decision" if n == 1 else "decisions"
        return f"{pct} · ~{n} {word} to {target_pct} (projected)"


class AttentionQueue(SqbylModel):
    """The routed result: what was auto-applied, what's queued, and the readiness signal.

    ``auto_applied`` is shown collapsed/done in the console (one-click undo); ``queue`` is the
    leverage-sorted set of decisions that need a human, highest-leverage first (spec §5.5)."""

    auto_applied: list[Decision] = Field(default_factory=list)
    queue: list[Decision] = Field(default_factory=list)
    readiness: ReadinessSignal

    @property
    def n_auto_applied(self) -> int:
        return len(self.auto_applied)

    @property
    def n_queued(self) -> int:
        return len(self.queue)
